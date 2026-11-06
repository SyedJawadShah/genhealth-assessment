#!/usr/bin/env bash
# Push a local image to a Lightsail container service, deploy it, and verify the new
# version is live. Used by .github/workflows/deploy.yml; also runnable from an x86_64 host.
#
# Usage: scripts/deploy_lightsail.sh <local-image:tag> <git-sha>
# Env:   AWS_REGION, SERVICE_NAME, AWS credentials; optional DEPLOY_TIMEOUT_SECONDS and
#        APP_ENV_JSON (JSON object of extra app settings, e.g. secrets)
# Needs: aws CLI v2, lightsailctl, docker, jq, curl
set -euo pipefail

usage="usage: deploy_lightsail.sh <local-image:tag> <git-sha>"
image="${1:?$usage}"
sha="${2:?$usage}"
: "${AWS_REGION:?AWS_REGION is required}"
: "${SERVICE_NAME:?SERVICE_NAME is required}"
timeout_seconds="${DEPLOY_TIMEOUT_SECONDS:-900}"
container="app"
port=8000

lightsail() { aws lightsail --region "$AWS_REGION" "$@"; }

# A new service stays PENDING for a few minutes, and a service mid-deployment rejects
# another deployment, so wait until it can accept one.
deadline=$((SECONDS + timeout_seconds))
while true; do
  service_state=$(lightsail get-container-services --service-name "$SERVICE_NAME" \
    --query 'containerServices[0].state' --output text)
  case "$service_state" in
    READY | RUNNING) break ;;
    DISABLED | DELETING)
      echo "Container service $SERVICE_NAME is $service_state; cannot deploy." >&2
      exit 1
      ;;
  esac
  if ((SECONDS >= deadline)); then
    echo "Timed out waiting for $SERVICE_NAME to be ready (state: $service_state)" >&2
    exit 1
  fi
  echo "Service is $service_state; waiting"
  sleep 15
done

echo "Pushing $image to $SERVICE_NAME"
push_output=$(lightsail push-container-image \
  --service-name "$SERVICE_NAME" --label "$container" --image "$image")
# The CLI prints: Refer to this image as ":<service>.<label>.<n>" in deployments.
registered=$(grep -oE '":[^"]+"' <<<"$push_output" | tail -n 1 | tr -d '"' || true)
if [[ -z "$registered" ]]; then
  echo "Could not find the registered image name in the push output:" >&2
  echo "$push_output" >&2
  exit 1
fi
echo "Registered $registered"

# App settings, secrets included, come from the APP_ENV_JSON secret: a JSON object of string
# values. They reach Lightsail through a private temp file, never the command line or the log.
export APP_ENV_JSON="${APP_ENV_JSON:-}"
if [[ -n "$APP_ENV_JSON" ]] && ! jq -e 'type == "object" and all(.[]; type == "string")' \
  <<<"$APP_ENV_JSON" >/dev/null 2>&1; then
  echo "APP_ENV_JSON must be a JSON object whose values are all strings" >&2
  exit 1
fi
if [[ -n "$APP_ENV_JSON" ]]; then
  echo "App settings from APP_ENV_JSON: $(jq -r 'keys | join(", ")' <<<"$APP_ENV_JSON")"
fi
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
(
  umask 077
  jq -n --arg name "$container" --arg image "$registered" --arg sha "$sha" --arg port "$port" \
    '(env.APP_ENV_JSON | if . == "" then {} else fromjson end) as $extra
     | {($name): {image: $image, ports: {($port): "HTTP"},
                  environment: ({LOG_LEVEL: "INFO"} + $extra
                                + {APP_ENV: "production", GIT_SHA: $sha})}}' \
    >"$workdir/containers.json"
)
endpoint=$(jq -n --arg name "$container" --argjson port "$port" \
  '{containerName: $name, containerPort: $port,
    healthCheck: {path: "/healthz", intervalSeconds: 10, timeoutSeconds: 5,
                  healthyThreshold: 2, unhealthyThreshold: 3, successCodes: "200"}}')

version=$(lightsail create-container-service-deployment --service-name "$SERVICE_NAME" \
  --containers "file://$workdir/containers.json" --public-endpoint "$endpoint" \
  --query 'containerService.nextDeployment.version' --output text)
echo "Created deployment version $version; waiting for it to become active"

deadline=$((SECONDS + timeout_seconds))
while true; do
  state=$(lightsail get-container-service-deployments --service-name "$SERVICE_NAME" \
    --query "deployments[?version==\`${version}\`].state | [0]" --output text)
  case "$state" in
    ACTIVE) break ;;
    FAILED)
      echo "Deployment $version failed health checks; the previous version is still serving." >&2
      echo "Inspect logs: aws lightsail get-container-log --region $AWS_REGION" \
        "--service-name $SERVICE_NAME --container-name $container" >&2
      exit 1
      ;;
  esac
  if ((SECONDS >= deadline)); then
    echo "Timed out after ${timeout_seconds}s waiting for deployment $version (state: $state)" >&2
    exit 1
  fi
  sleep 15
done

url=$(lightsail get-container-services --service-name "$SERVICE_NAME" \
  --query 'containerServices[0].url' --output text)
url="${url%/}"
for _ in $(seq 1 20); do
  if curl -fsS --max-time 10 "$url/healthz" | jq -e --arg sha "$sha" '.version == $sha' >/dev/null
  then
    echo "Live: $url (version $sha)"
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
      echo "url=$url" >>"$GITHUB_OUTPUT"
    fi
    exit 0
  fi
  sleep 6
done
echo "Deployment $version is active, but $url/healthz does not report version $sha" >&2
exit 1
