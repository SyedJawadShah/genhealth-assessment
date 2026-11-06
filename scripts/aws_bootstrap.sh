#!/usr/bin/env bash
# One-time AWS setup so GitHub Actions can deploy to a Lightsail container service.
#
# Creates, or updates if they already exist:
#   1. the GitHub OIDC identity provider in IAM
#   2. an IAM role that GitHub Actions can assume from the main branch or the "production"
#      environment, limited to the Lightsail container actions the deploy script needs
#   3. a Lightsail container service
#
# COST: a container service is billed every month from creation until it is deleted, even
# with nothing deployed (micro: $10/month per node when this was written; check
# https://aws.amazon.com/lightsail/pricing/).
#
# Run from a shell with admin AWS credentials, or paste into AWS CloudShell:
#   GITHUB_OWNER=<github-user> AWS_REGION=us-east-2 scripts/aws_bootstrap.sh
#
# Optional env:
#   GITHUB_REPO   repo allowed to deploy (default "*": any repo owned by GITHUB_OWNER)
#   GITHUB_OWNER_ID  numeric GitHub account ID (default: looked up from the GitHub API)
#   SERVICE_NAME  Lightsail container service name (default "service")
#   POWER         nano|micro|small|medium|large|xlarge (default "micro"; changeable later)
#   ROLE_NAME     IAM role name (default "github-deploy-lightsail")
set -euo pipefail

: "${GITHUB_OWNER:?GITHUB_OWNER is required (your GitHub user or organization)}"
: "${AWS_REGION:?AWS_REGION is required, for example us-east-2}"
GITHUB_REPO="${GITHUB_REPO:-*}"
SERVICE_NAME="${SERVICE_NAME:-service}"
POWER="${POWER:-micro}"
ROLE_NAME="${ROLE_NAME:-github-deploy-lightsail}"
OIDC_HOST="token.actions.githubusercontent.com"

for tool in aws jq curl; do
  command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }
done

account=$(aws sts get-caller-identity --query Account --output text)
provider_arn="arn:aws:iam::${account}:oidc-provider/${OIDC_HOST}"
echo "Account $account, region $AWS_REGION"

if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$provider_arn" \
  >/dev/null 2>&1; then
  echo "GitHub OIDC provider already exists"
else
  aws iam create-open-id-connect-provider --url "https://${OIDC_HOST}" \
    --client-id-list sts.amazonaws.com >/dev/null
  echo "Created GitHub OIDC provider"
fi

# GitHub's token subject names the repository either as "owner/repo" or, with immutable
# subjects, as "owner@<owner-id>/repo@<repo-id>". The numeric IDs pin the real account, so a
# look-alike or re-registered username cannot match. Each form ends with the environment
# when the job's environment applies, or with the branch when it does not (deployment
# environments are unavailable for private repos on free plans).
if [[ -z "${GITHUB_OWNER_ID:-}" ]]; then
  GITHUB_OWNER_ID=$(curl -fsS "https://api.github.com/users/${GITHUB_OWNER}" | jq -r .id)
fi
[[ "$GITHUB_OWNER_ID" =~ ^[0-9]+$ ]] || { echo "Could not look up GITHUB_OWNER_ID" >&2; exit 1; }
if [[ "$GITHUB_REPO" == "*" ]]; then immutable_repo="*"; else immutable_repo="${GITHUB_REPO}@*"; fi
subjects=$(jq -n \
  --arg legacy "repo:${GITHUB_OWNER}/${GITHUB_REPO}" \
  --arg immutable "repo:${GITHUB_OWNER}@${GITHUB_OWNER_ID}/${immutable_repo}" \
  '[$immutable, $legacy] | map(. + ":environment:production", . + ":ref:refs/heads/main")')
trust=$(jq -n --arg provider "$provider_arn" --arg host "$OIDC_HOST" \
  --argjson subjects "$subjects" '{
  Version: "2012-10-17",
  Statement: [{
    Effect: "Allow",
    Principal: {Federated: $provider},
    Action: "sts:AssumeRoleWithWebIdentity",
    Condition: {
      StringEquals: {("\($host):aud"): "sts.amazonaws.com"},
      StringLike: {("\($host):sub"): $subjects}
    }
  }]
}')
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$trust"
  echo "Updated trust policy on role $ROLE_NAME"
else
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$trust" \
    --max-session-duration 3600 --description "GitHub Actions deploys to Lightsail" >/dev/null
  echo "Created role $ROLE_NAME"
fi
echo "Deploys allowed from: $(jq -r 'join(", ")' <<<"$subjects")"

permissions=$(jq -n '{
  Version: "2012-10-17",
  Statement: [{
    Sid: "LightsailContainerDeploy",
    Effect: "Allow",
    Action: [
      "lightsail:CreateContainerServiceRegistryLogin",
      "lightsail:RegisterContainerImage",
      "lightsail:CreateContainerServiceDeployment",
      "lightsail:GetContainerServices",
      "lightsail:GetContainerServiceDeployments",
      "lightsail:GetContainerImages",
      "lightsail:GetContainerLog"
    ],
    Resource: "*"
  }]
}')
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name lightsail-container-deploy \
  --policy-document "$permissions"
echo "Attached Lightsail deploy policy"

if aws lightsail get-container-services --region "$AWS_REGION" \
  --service-name "$SERVICE_NAME" >/dev/null 2>&1; then
  echo "Container service $SERVICE_NAME already exists"
else
  aws lightsail create-container-service --region "$AWS_REGION" \
    --service-name "$SERVICE_NAME" --power "$POWER" --scale 1 >/dev/null
  echo "Created container service $SERVICE_NAME ($POWER x1); it takes a few minutes to be ready"
fi

role_arn=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
cat <<EOF

Done. In the GitHub repository, set these Actions variables (none is a credential):

  gh variable set AWS_DEPLOY_ROLE_ARN --body "$role_arn"
  gh variable set AWS_REGION --body "$AWS_REGION"
  gh variable set LIGHTSAIL_SERVICE_NAME --body "$SERVICE_NAME"

Then push to main, or run the Deploy workflow from the Actions tab.

To stop billing:
  aws lightsail delete-container-service --region $AWS_REGION --service-name $SERVICE_NAME
EOF
