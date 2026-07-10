/**
 * Synth-level verification of PR 5 observability (design-doc §7).
 *
 * Asserts the monitoring stack's alarm/dashboard shape, that every alarm notifies
 * the alerts topic, that the agent roles can publish the opt-in client metrics, and
 * that the TTL Lambda picks up a boto3 layer when `-c boto3LayerArn=...` is set (the
 * deploy prerequisite before flipping DRY_RUN off).
 *
 * Run: `npm test` (ts-node). Throws + exits non-zero on any mismatch.
 */
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { MemoryStack } from '../lib/memory-stack';
import { MonitoringStack } from '../lib/monitoring-stack';
import { DASHBOARD_NAME, METRIC_NAMESPACE_CLIENT } from '../lib/config';

const mon = Template.fromStack(new MonitoringStack(new cdk.App(), 'MonVerify'));

// MemoryStack synthesized twice: once plain, once with the boto3 layer context.
const memPlain = Template.fromStack(new MemoryStack(new cdk.App(), 'MemPlain'));
const LAYER_ARN = 'arn:aws:lambda:us-west-2:123456789012:layer:boto3-s3vectors:3';
const memLayer = Template.fromStack(
  new MemoryStack(new cdk.App({ context: { boto3LayerArn: LAYER_ARN } }), 'MemLayer'),
);
const memDryRunOff = Template.fromStack(
  new MemoryStack(new cdk.App({ context: { ttlDryRun: false } }), 'MemDryRunOff'),
);

const EXPECTED_ALARMS = 8;

const checks: Array<[string, () => void]> = [
  // --- Alarm inventory + dashboard -------------------------------------------
  [`exactly ${EXPECTED_ALARMS} CloudWatch alarms`, () =>
    mon.resourceCountIs('AWS::CloudWatch::Alarm', EXPECTED_ALARMS)],

  ['one dashboard named VectorVault', () => {
    mon.resourceCountIs('AWS::CloudWatch::Dashboard', 1);
    mon.hasResourceProperties('AWS::CloudWatch::Dashboard', { DashboardName: DASHBOARD_NAME });
  }],

  // --- §7 alarms are wired to real metrics -----------------------------------
  ['TTL Lambda errors alarm (AWS/Lambda Errors > 0)', () =>
    mon.hasResourceProperties('AWS::CloudWatch::Alarm', {
      Namespace: 'AWS/Lambda',
      MetricName: 'Errors',
      Threshold: 0,
      ComparisonOperator: 'GreaterThanThreshold',
    })],

  ['S3 Vectors 429 alarm (VectorVault/Client ThrottleException > 10)', () =>
    mon.hasResourceProperties('AWS::CloudWatch::Alarm', {
      Namespace: METRIC_NAMESPACE_CLIENT,
      MetricName: 'ThrottleException',
      Threshold: 10,
    })],

  ['QueryVectors p95 latency alarm (> 2000 ms, p95)', () =>
    mon.hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'QueryLatencyMs',
      ExtendedStatistic: 'p95',
      Threshold: 2000,
    })],

  // --- every alarm notifies the alerts topic (SNS action) --------------------
  ['every alarm has an SNS AlarmAction', () => {
    const alarms = mon.findResources('AWS::CloudWatch::Alarm');
    const missing = Object.entries(alarms).filter(
      ([, r]) => !(r.Properties?.AlarmActions?.length > 0),
    );
    if (missing.length > 0) {
      throw new Error(`${missing.length} alarm(s) have no AlarmActions: ${missing.map(([k]) => k).join(', ')}`);
    }
  }],

  // --- agent roles can publish the opt-in client metrics ---------------------
  ['planner + researcher may PutMetricData for VectorVault/Client only', () => {
    const policies = memPlain.findResources('AWS::IAM::Policy');
    const withMetrics = Object.values(policies).filter((p) => {
      const doc = JSON.stringify(p.Properties?.PolicyDocument ?? {});
      return doc.includes('cloudwatch:PutMetricData') && doc.includes(METRIC_NAMESPACE_CLIENT);
    });
    if (withMetrics.length < 2) {
      throw new Error(`expected >=2 roles with scoped PutMetricData, found ${withMetrics.length}`);
    }
  }],

  // --- boto3 layer finalization (context-gated) ------------------------------
  ['TTL Lambda has NO layer by default (safe first deploy)', () =>
    memPlain.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'vectorvault.ttl_worker.handler',
      Layers: Match.absent(),
    })],

  ['TTL Lambda attaches the boto3 layer when -c boto3LayerArn is set', () =>
    memLayer.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'vectorvault.ttl_worker.handler',
      Layers: Match.arrayWith([LAYER_ARN]),
    })],

  // --- DRY_RUN context flag --------------------------------------------------
  ['TTL Lambda DRY_RUN defaults to true', () =>
    memPlain.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'vectorvault.ttl_worker.handler',
      Environment: Match.objectLike({ Variables: Match.objectLike({ DRY_RUN: 'true' }) }),
    })],

  ['TTL Lambda DRY_RUN=false when -c ttlDryRun=false', () =>
    memDryRunOff.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'vectorvault.ttl_worker.handler',
      Environment: Match.objectLike({ Variables: Match.objectLike({ DRY_RUN: 'false' }) }),
    })],
];

let failed = 0;
for (const [label, check] of checks) {
  try {
    check();
    console.log(`  ok   ${label}`);
  } catch (err) {
    failed += 1;
    console.error(`  FAIL ${label}\n       ${(err as Error).message.split('\n')[0]}`);
  }
}

if (failed > 0) {
  console.error(`\nmonitoring verification FAILED: ${failed} check(s)`);
  process.exit(1);
}
console.log(`\nmonitoring verification OK: ${checks.length} checks passed`);
