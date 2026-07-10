#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { MemoryStack } from '../lib/memory-stack';
import { MonitoringStack } from '../lib/monitoring-stack';

const app = new cdk.App();

// Env-aware at deploy (account/Region come from the active AWS profile); falls
// back to env-agnostic for `cdk synth` in CI, which has no credentials.
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

new MemoryStack(app, 'VectorVaultMemoryStack', {
  env,
  description:
    'VectorVault PR 1 - S3 Vectors shared memory: vector bucket + indexes, content bucket, ' +
    'DynamoDB, IAM roles, KMS, CloudTrail write data events, $20 AWS Budget, SSM config.',
});

// PR 5 monitoring. Decoupled from MemoryStack (references resources by name + the
// alerts topic from SSM), so it deploys/destroys independently and never mutates
// the one-way-door stack. Deploy: `cdk deploy VectorVaultMonitoringStack`.
new MonitoringStack(app, 'VectorVaultMonitoringStack', {
  env,
  description: 'VectorVault PR 5 - CloudWatch dashboard + alarms (design-doc §7).',
});

cdk.Tags.of(app).add('project', 'vectorvault');
