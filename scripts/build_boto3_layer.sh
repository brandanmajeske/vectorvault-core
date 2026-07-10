#!/usr/bin/env bash
# Build + publish a boto3 Lambda layer for the TTL worker (PR 5 finalization).
#
# The Python 3.12 Lambda runtime bundles an older boto3 that may lack the s3vectors
# client. The TTL worker ships DRY_RUN=true so the first deploy is a safe no-op, but
# before flipping DRY_RUN=false you must give it a boto3 >= 1.43.31 that speaks
# s3vectors. This script builds that layer and prints its ARN; pass the ARN to CDK:
#
#   ARN=$(AWS_PROFILE=<your-profile> scripts/build_boto3_layer.sh)
#   cd infra && AWS_PROFILE=<your-profile> npx cdk deploy VectorVaultMemoryStack -c boto3LayerArn="$ARN"
#
# Kept OUT of `cdk synth` on purpose: CI synth runs without Docker/pip, so bundling
# the layer at synth time would break it. This runs manually, once, when finalizing.
#
# Requires: python3.12, pip, and the AWS CLI (authenticated; e.g. `aws sso login`).
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
BOTO3_VERSION="${BOTO3_VERSION:-1.43.31}"
LAYER_NAME="${LAYER_NAME:-vectorvault-boto3}"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# Lambda expects layer libs under python/ on the runtime path.
pip install --quiet --only-binary=:all: \
  --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp \
  --target "$WORKDIR/python" "boto3>=${BOTO3_VERSION}" "botocore>=${BOTO3_VERSION}" >&2

( cd "$WORKDIR" && zip -qr layer.zip python )

ARN=$(aws lambda publish-layer-version \
  --layer-name "$LAYER_NAME" \
  --description "boto3>=${BOTO3_VERSION} with s3vectors client (VectorVault TTL worker)" \
  --license-info "Apache-2.0" \
  --compatible-runtimes python3.12 \
  --compatible-architectures x86_64 \
  --zip-file "fileb://$WORKDIR/layer.zip" \
  --region "$REGION" \
  --query 'LayerVersionArn' --output text)

echo "published: $ARN" >&2   # human-readable to stderr
echo "$ARN"                  # machine-readable to stdout (capture with $(...))
