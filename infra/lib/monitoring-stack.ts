import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cw_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ssm from 'aws-cdk-lib/aws-ssm';

import {
  DASHBOARD_NAME,
  EMBED_MODEL_ID,
  METRIC_NAMESPACE_CLIENT,
  METRIC_NAMESPACE_TTL,
  ssmParam,
  TTL_DLQ_NAME,
  TTL_WORKER_FUNCTION_NAME,
} from './config';

/**
 * VectorVault PR 5 — CloudWatch dashboard + alarms (design-doc §7).
 *
 * Deliberately decoupled from {@link MemoryStack}: it references resources by their
 * stable names (config.ts) and imports the alerts SNS topic ARN from the SSM
 * contract PR 1 publishes, so it never adds a cross-stack export to the deployed,
 * one-way-door MemoryStack. Deploy independently: `cdk deploy VectorVaultMonitoringStack`.
 *
 * Alarm coverage maps design-doc §7. Rows not backed by an emitted metric are noted
 * inline: the AWS budget alarm already lives in MemoryStack (AWS Budgets, PR 1);
 * "vectors per index > 10M" and "Bedrock token rate > 2× baseline" need custom
 * metrics not yet emitted and are deferred (documented, not silently dropped).
 *
 * The VectorVault/Client alarms treat missing data as NOT_BREACHING because client
 * metric emission is opt-in (enable_metrics=True); they activate automatically once
 * an agent starts emitting, with no alarm churn while it is off.
 */
export class MonitoringStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Alerts topic created by MemoryStack; referenced by ARN from the SSM contract.
    const topicArn = ssm.StringParameter.valueForStringParameter(this, ssmParam('alerts-topic-arn'));
    const alertsTopic = sns.Topic.fromTopicArn(this, 'AlertsTopic', topicArn);
    const onAlarm = new cw_actions.SnsAction(alertsTopic);

    const alarms: cloudwatch.IAlarm[] = [];
    const alarm = (
      metric: cloudwatch.IMetric,
      id: string,
      opts: {
        threshold: number;
        comparison: cloudwatch.ComparisonOperator;
        evaluationPeriods?: number;
        description: string;
        missing?: cloudwatch.TreatMissingData;
      },
    ): void => {
      const a = new cloudwatch.Alarm(this, id, {
        metric,
        threshold: opts.threshold,
        comparisonOperator: opts.comparison,
        evaluationPeriods: opts.evaluationPeriods ?? 1,
        alarmDescription: opts.description,
        treatMissingData: opts.missing ?? cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      a.addAlarmAction(onAlarm);
      a.addOkAction(onAlarm);
      alarms.push(a);
    };

    const GT = cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD;
    const LT = cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD;

    // --- TTL worker (AWS/Lambda + AWS/SQS + VectorVault/TTL) ------------------
    const fnDims = { FunctionName: TTL_WORKER_FUNCTION_NAME };
    const lambdaMetric = (name: string, statistic: string, period: cdk.Duration) =>
      new cloudwatch.Metric({ namespace: 'AWS/Lambda', metricName: name, dimensionsMap: fnDims, statistic, period });

    const ttlErrors = lambdaMetric('Errors', 'Sum', cdk.Duration.hours(1));
    const ttlDuration = lambdaMetric('Duration', 'p95', cdk.Duration.hours(1));
    const ttlInvocations = lambdaMetric('Invocations', 'Sum', cdk.Duration.days(1));

    // TTL Lambda failures — any (design-doc §7: expired vectors accumulate).
    alarm(ttlErrors, 'TtlLambdaErrors', {
      threshold: 0, comparison: GT,
      description: 'TTL worker Lambda invocation errors — expired vectors may accumulate.',
    });

    // Anything landing in the DLQ means a run failed after retries.
    const dlqDepth = new cloudwatch.Metric({
      namespace: 'AWS/SQS', metricName: 'ApproximateNumberOfMessagesVisible',
      dimensionsMap: { QueueName: TTL_DLQ_NAME }, statistic: 'Maximum', period: cdk.Duration.hours(1),
    });
    alarm(dlqDepth, 'TtlDlqNotEmpty', {
      threshold: 0, comparison: GT,
      description: 'Messages in the TTL dead-letter queue — a lifecycle sweep failed.',
    });

    const ttlMetric = (name: string, statistic = 'Sum', period = cdk.Duration.days(1)) =>
      new cloudwatch.Metric({ namespace: METRIC_NAMESPACE_TTL, metricName: name, statistic, period });
    const deletedCount = ttlMetric('DeletedCount');
    const archivedCount = ttlMetric('ArchivedCount');
    const driftRepaired = ttlMetric('DriftRepaired');

    // Circuit-breaker backstop: alarm if a run deletes more than the absolute cap
    // (MAX_DELETE_ABS=1000). The worker also aborts itself; this makes it visible.
    alarm(deletedCount, 'TtlDeletionSpike', {
      threshold: 1000, comparison: GT,
      description: 'TTL deletions in a run exceeded the circuit-breaker floor (1,000).',
    });

    // --- Bedrock embedding errors (AWS/Bedrock) ------------------------------
    const bedrockDims = { ModelId: EMBED_MODEL_ID };
    const bedrockClientErr = new cloudwatch.Metric({
      namespace: 'AWS/Bedrock', metricName: 'InvocationClientErrors',
      dimensionsMap: bedrockDims, statistic: 'Sum', period: cdk.Duration.hours(1),
    });
    const bedrockServerErr = new cloudwatch.Metric({
      namespace: 'AWS/Bedrock', metricName: 'InvocationServerErrors',
      dimensionsMap: bedrockDims, statistic: 'Sum', period: cdk.Duration.hours(1),
    });
    const bedrockErrors = new cloudwatch.MathExpression({
      expression: 'clientErr + serverErr',
      usingMetrics: { clientErr: bedrockClientErr, serverErr: bedrockServerErr },
      period: cdk.Duration.hours(1), label: 'BedrockEmbeddingErrors',
    });
    alarm(bedrockErrors, 'BedrockEmbeddingErrors', {
      threshold: 5, comparison: GT,
      description: 'Bedrock Titan embedding errors > 5/hour — check model access/throttling.',
    });

    // --- Client custom metrics (VectorVault/Client — opt-in) -----------------
    const clientMetric = (name: string, statistic: string, period: cdk.Duration) =>
      new cloudwatch.Metric({ namespace: METRIC_NAMESPACE_CLIENT, metricName: name, statistic, period });

    // S3 Vectors 429 rate > 10/min.
    alarm(clientMetric('ThrottleException', 'Sum', cdk.Duration.minutes(1)), 'S3VectorsThrottle', {
      threshold: 10, comparison: GT,
      description: 'S3 Vectors TooManyRequestsException > 10/min — enable backoff/batching.',
    });

    // QueryVectors p95 latency > 2s.
    const queryP95 = clientMetric('QueryLatencyMs', 'p95', cdk.Duration.minutes(5));
    alarm(queryP95, 'QueryLatencyP95', {
      threshold: 2000, comparison: GT, evaluationPeriods: 3,
      description: 'QueryVectors p95 latency > 2s — investigate index size / OpenSearch export.',
    });

    // InjectionSuspect rate > 10/day.
    alarm(clientMetric('InjectionSuspect', 'Sum', cdk.Duration.days(1)), 'InjectionSuspect', {
      threshold: 10, comparison: GT,
      description: 'Injection-suspect external writes > 10/day — review flagged memories.',
    });

    // EmbeddingCacheHitRate < 20% (hits / (hits + misses)).
    const cacheHit = clientMetric('EmbeddingCacheHit', 'Sum', cdk.Duration.hours(1));
    const cacheMiss = clientMetric('EmbeddingCacheMiss', 'Sum', cdk.Duration.hours(1));
    const hitRate = new cloudwatch.MathExpression({
      expression: '100 * hits / (hits + misses)',
      usingMetrics: { hits: cacheHit, misses: cacheMiss },
      period: cdk.Duration.hours(1), label: 'EmbeddingCacheHitRate %',
    });
    alarm(hitRate, 'EmbeddingCacheHitRateLow', {
      threshold: 20, comparison: LT, evaluationPeriods: 3,
      description: 'Embedding cache hit-rate < 20% — review agent loops / extend cache TTL.',
    });

    // =========================================================================
    // Dashboard (design-doc §7 "Dashboards").
    // =========================================================================
    const dashboard = new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: DASHBOARD_NAME,
      defaultInterval: cdk.Duration.days(3),
    });

    dashboard.addWidgets(
      new cloudwatch.AlarmStatusWidget({ title: 'Alarms', width: 24, height: 3, alarms }),
    );
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'TTL worker — lifecycle activity',
        left: [archivedCount, deletedCount, driftRepaired, ttlMetric('Errors')],
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        title: 'TTL worker — Lambda health',
        left: [ttlErrors, ttlInvocations],
        right: [ttlDuration, dlqDepth],
        width: 12,
      }),
    );
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Embedding — cache hit-rate % & Bedrock errors',
        left: [hitRate],
        right: [bedrockErrors],
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        title: 'Query — p95 latency (ms) & 429 throttles',
        left: [queryP95],
        right: [clientMetric('ThrottleException', 'Sum', cdk.Duration.minutes(5))],
        width: 12,
      }),
    );
    dashboard.addWidgets(
      new cloudwatch.SingleValueWidget({
        title: 'Injection-suspect writes (external, per day)',
        metrics: [clientMetric('InjectionSuspect', 'Sum', cdk.Duration.days(1))],
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        // Write-path dedup outcomes (design-doc §7): new facts vs corrections vs
        // dedup no-ops (exact-dup UNCHANGED + near-dup DUPLICATE_DETECTED).
        title: 'Write-path — created / superseded / deduped',
        left: [
          clientMetric('StoreCreated', 'Sum', cdk.Duration.hours(1)),
          clientMetric('StoreSuperseded', 'Sum', cdk.Duration.hours(1)),
          clientMetric('StoreUnchanged', 'Sum', cdk.Duration.hours(1)),
          clientMetric('StoreDuplicateDetected', 'Sum', cdk.Duration.hours(1)),
        ],
        width: 12,
      }),
    );

    new cdk.CfnOutput(this, 'DashboardName', { value: DASHBOARD_NAME });
    new cdk.CfnOutput(this, 'AlarmCount', { value: String(alarms.length) });

    cdk.Tags.of(this).add('project', 'vectorvault');
  }
}
