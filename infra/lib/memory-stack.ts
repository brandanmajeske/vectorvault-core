import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3vectors from 'aws-cdk-lib/aws-s3vectors';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as cloudtrail from 'aws-cdk-lib/aws-cloudtrail';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';

import {
  ALERTS_TOPIC_NAME,
  CONTENT_BUCKET_NAME_PREFIX,
  DATA_TYPE,
  DISTANCE_METRIC,
  EMBED_CACHE_TABLE,
  EMBED_CACHE_TTL_ATTRIBUTE,
  EMBED_MODEL_ID,
  INDEX_NAMES,
  MEMORY_INDEX_TABLE,
  MEMORY_INDEX_TASK_GSI,
  METRIC_NAMESPACE_CLIENT,
  METRIC_NAMESPACE_TTL,
  NON_FILTERABLE_METADATA_KEYS,
  PROJECT_TAG_KEY,
  PROJECT_TAG_VALUE,
  ssmParam,
  TRAIL_LOG_BUCKET_NAME_PREFIX,
  TTL_DLQ_NAME,
  TTL_INDEX_TABLE,
  TTL_WORKER_FUNCTION_NAME,
  VECTOR_BUCKET_NAME,
  VECTOR_DIMENSION,
} from './config';

/**
 * VectorVault PR 1 — all AWS resources the Python memory client (PR 2) depends on.
 *
 * Resource inventory follows implementation-plan.md PR 1 and design-doc §2/§5:
 *   KMS key · S3 Vectors bucket + 3 indexes · S3 content bucket · 2 DynamoDB tables
 *   · 4 IAM roles · CloudTrail write-only data events · $20 AWS Budget · SSM config.
 *
 * See README.md for the one-way-door checklist (KMS/metadata schema/CloudTrail are
 * immutable after creation) and the deploy/teardown workflow.
 */
export class MemoryStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // --- Context flags -------------------------------------------------------
    // retainData=true flips data stores to RETAIN for production; default DESTROY
    // keeps dev teardown clean and avoids orphaned KMS keys (~$1/mo each — the
    // owner enforces a $20/mo cap). See README teardown note.
    const retainData = this.node.tryGetContext('retainData') === true;
    const removalPolicy = retainData ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY;
    const autoDeleteObjects = !retainData;

    // Who may assume the agent roles. Default: account root (any in-account principal
    // with sts:AssumeRole — the standard dev pattern). Narrow it with
    // `-c trustedPrincipalArn=...` (claude-review Q6): one or more comma-separated ARN
    // *patterns* matched against aws:PrincipalArn as a trust-policy condition — the
    // Principal element itself can't hold wildcards, and an exact SSO-role ARN would
    // break when the permission set re-provisions, so a pattern like
    //   arn:aws:iam::<acct>:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*
    // is the robust way to pin assumption to the owner's SSO role.
    const trustedPrincipalArn: string | undefined = this.node.tryGetContext('trustedPrincipalArn');
    // ENFORCE source identity (design-doc §5): every assumption of a human role MUST
    // carry an sts:SourceIdentity (the real AWS principal, set by the client at
    // assume time). This upgrades agent attribution from self-asserted to
    // IAM-enforced — a session with no source identity is rejected at AssumeRole, and
    // the value is sticky + immutable for the session's life and propagates to every
    // downstream CloudTrail event. Folded into the trust principal so the condition
    // lands on the generated AssumeRole statement.
    const trustConditions: Record<string, Record<string, unknown>> = {
      StringLike: { 'sts:SourceIdentity': '*' }, // require present (non-empty)
    };
    if (trustedPrincipalArn) {
      trustConditions.ArnLike = {
        'aws:PrincipalArn': trustedPrincipalArn.split(',').map((s) => s.trim()),
      };
    }
    const trustPrincipal: iam.IPrincipal = new iam.PrincipalWithConditions(
      new iam.AccountRootPrincipal(),
      trustConditions,
    );

    const alertEmail: string = this.node.tryGetContext('alertEmail') ?? 'alerts@example.com'; // REPLACE: -c alertEmail=you@company.com
    // Monthly hard cost cap in USD (design-doc §6). Tune per deployment: -c budgetUsd=250
    const budgetUsd: number = Number(this.node.tryGetContext('budgetUsd') ?? 20);

    const partition = cdk.Aws.PARTITION;
    const region = cdk.Aws.REGION;
    const account = cdk.Aws.ACCOUNT_ID;
    const nameSuffix = `${account}-${region}`;

    // =========================================================================
    // KMS — SSE-KMS for the vector bucket, content bucket, and DynamoDB.
    // One-way door (design-doc §5): the vector bucket's encryption config is
    // immutable, so this key must exist with the right policy before first deploy.
    // =========================================================================
    const key = new kms.Key(this, 'MemoryKey', {
      alias: 'alias/vectorvault-memory',
      description: 'VectorVault SSE-KMS key (vector bucket, content bucket, DynamoDB)',
      enableKeyRotation: true,
      removalPolicy,
    });

    // ONE-WAY DOOR: S3 Vectors indexing service must be able to use the key or
    // indexing breaks (claude-review appendix #6). Decrypt is the documented
    // minimum; GenerateDataKey/DescribeKey cover the SSE-KMS write path safely.
    key.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowS3VectorsIndexingUseOfKey',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('indexing.s3vectors.amazonaws.com')],
        actions: ['kms:Decrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
        resources: ['*'], // resource policy scoped to this key
      }),
    );

    // =========================================================================
    // S3 Vectors — vector bucket + three indexes (design-doc §2).
    // =========================================================================
    const vectorBucket = new s3vectors.CfnVectorBucket(this, 'VectorBucket', {
      vectorBucketName: VECTOR_BUCKET_NAME,
      // ONE-WAY DOOR: full KMS key ARN (not alias/ID), same-Region key.
      encryptionConfiguration: {
        sseType: 'aws:kms',
        kmsKeyArn: key.keyArn,
      },
      tags: [{ key: PROJECT_TAG_KEY, value: PROJECT_TAG_VALUE }],
    });
    vectorBucket.applyRemovalPolicy(removalPolicy);

    const createIndex = (logicalId: string, indexName: string): s3vectors.CfnIndex => {
      const index = new s3vectors.CfnIndex(this, logicalId, {
        // vectorBucketArn creates the CloudFormation dependency edge on the bucket
        // and encodes the bucket name (arn:...:bucket/<name>).
        vectorBucketArn: vectorBucket.attrVectorBucketArn,
        indexName,
        dataType: DATA_TYPE,
        dimension: VECTOR_DIMENSION,
        distanceMetric: DISTANCE_METRIC,
        // ONE-WAY DOOR: non-filterable key list is fixed at creation (config.ts).
        metadataConfiguration: {
          nonFilterableMetadataKeys: NON_FILTERABLE_METADATA_KEYS,
        },
        tags: [{ key: PROJECT_TAG_KEY, value: PROJECT_TAG_VALUE }],
      });
      index.applyRemovalPolicy(removalPolicy);
      return index;
    };

    const sharedIndex = createIndex('SharedTeamMemoryIndex', INDEX_NAMES.shared);
    const plannerIndex = createIndex('PrivatePlannerIndex', INDEX_NAMES.planner);
    const researcherIndex = createIndex('PrivateResearcherIndex', INDEX_NAMES.researcher);

    // =========================================================================
    // Content bucket — standard S3 for payloads > 30 KB (design-doc §2).
    // S3 versioning enabled as a recovery lever (claude-review O1).
    // =========================================================================
    const contentBucket = new s3.Bucket(this, 'ContentBucket', {
      bucketName: `${CONTENT_BUCKET_NAME_PREFIX}-${nameSuffix}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: key,
      enforceSSL: true,
      versioned: true,
      removalPolicy,
      autoDeleteObjects,
    });

    // =========================================================================
    // DynamoDB — embedding cache + canonical/listing index (design-doc §2, §4.2).
    // PITR enabled on both (recovery lever, claude-review O1).
    // =========================================================================
    const embedCacheTable = new dynamodb.Table(this, 'EmbedCacheTable', {
      tableName: EMBED_CACHE_TABLE,
      partitionKey: { name: 'content_hash', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: key,
      timeToLiveAttribute: EMBED_CACHE_TTL_ATTRIBUTE,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    const memoryIndexTable = new dynamodb.Table(this, 'MemoryIndexTable', {
      tableName: MEMORY_INDEX_TABLE,
      partitionKey: { name: 'canonical_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: key,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });
    // Backs list_memories by task_id, sorted by created_at (design-doc §4).
    memoryIndexTable.addGlobalSecondaryIndex({
      indexName: MEMORY_INDEX_TASK_GSI,
      partitionKey: { name: 'task_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Hard-TTL expiry index (PR 3): store_memory writes (index_name, expires_at, key)
    // so the TTL worker finds expired keys without a full scan (design-doc §2).
    const ttlIndexTable = new dynamodb.Table(this, 'TtlIndexTable', {
      tableName: TTL_INDEX_TABLE,
      partitionKey: { name: 'index_name', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'expires_at', type: dynamodb.AttributeType.NUMBER },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: key,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    // =========================================================================
    // IAM roles (design-doc §5). Index isolation is the security boundary;
    // metadata filters are NOT. Agents assume these with roleSessionName=agent_id
    // so CloudTrail attributes each call (design-doc §5; client-set at assume time).
    // =========================================================================
    const titanModelArn = `arn:${partition}:bedrock:${region}::foundation-model/${EMBED_MODEL_ID}`;

    const vectorActions = (actions: string[], indexes: s3vectors.CfnIndex[]) =>
      new iam.PolicyStatement({
        actions,
        resources: indexes.map((i) => i.attrIndexArn),
      });

    const bedrockEmbed = () =>
      new iam.PolicyStatement({
        sid: 'BedrockTitanEmbed',
        actions: ['bedrock:InvokeModel'],
        resources: [titanModelArn],
      });

    // Optional client-side metrics (design-doc §7): agents built with
    // enable_metrics=True publish the VectorVault/Client custom metrics the PR 5
    // monitoring alarms consume. Scoped to that namespace; unused when disabled.
    const clientMetrics = () =>
      new iam.PolicyStatement({
        sid: 'ClientMetrics',
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'], // PutMetricData does not support resource-level scoping
        conditions: { StringEquals: { 'cloudwatch:namespace': METRIC_NAMESPACE_CLIENT } },
      });

    // Content bucket: append-only Put/Get, prefix-scoped per role (claude-review S10).
    const contentReadWrite = (prefixes: string[]) =>
      new iam.PolicyStatement({
        sid: 'ContentReadWrite',
        actions: ['s3:PutObject', 's3:GetObject'],
        resources: prefixes.map((p) => `${contentBucket.bucketArn}/${p}/*`),
      });

    const RW_VECTORS = ['s3vectors:PutVectors', 's3vectors:QueryVectors', 's3vectors:GetVectors', 's3vectors:ListVectors'];
    const RO_VECTORS = ['s3vectors:QueryVectors', 's3vectors:GetVectors', 's3vectors:ListVectors'];
    const TTL_VECTORS = ['s3vectors:DeleteVectors', 's3vectors:ListVectors', 's3vectors:GetVectors', 's3vectors:PutVectors'];

    // --- Planner: shared-team-memory + private-planner (read/write) ----------
    const plannerRole = new iam.Role(this, 'MemoryPlannerRole', {
      roleName: 'MemoryPlannerRole',
      assumedBy: trustPrincipal,
      description: 'Planner agent - read/write shared-team-memory + private-planner',
    });
    plannerRole.addToPolicy(vectorActions(RW_VECTORS, [sharedIndex, plannerIndex]));
    plannerRole.addToPolicy(contentReadWrite([INDEX_NAMES.shared, INDEX_NAMES.planner]));
    plannerRole.addToPolicy(bedrockEmbed());
    plannerRole.addToPolicy(clientMetrics());
    embedCacheTable.grantReadWriteData(plannerRole);
    memoryIndexTable.grantReadWriteData(plannerRole);
    ttlIndexTable.grantWriteData(plannerRole); // store_memory writes hard-TTL rows
    key.grantEncryptDecrypt(plannerRole); // content-bucket SSE-KMS

    // --- Researcher: shared-team-memory + private-researcher (read/write) -----
    const researcherRole = new iam.Role(this, 'MemoryResearcherRole', {
      roleName: 'MemoryResearcherRole',
      assumedBy: trustPrincipal,
      description: 'Researcher agent - read/write shared-team-memory + private-researcher',
    });
    researcherRole.addToPolicy(vectorActions(RW_VECTORS, [sharedIndex, researcherIndex]));
    researcherRole.addToPolicy(contentReadWrite([INDEX_NAMES.shared, INDEX_NAMES.researcher]));
    researcherRole.addToPolicy(bedrockEmbed());
    researcherRole.addToPolicy(clientMetrics());
    embedCacheTable.grantReadWriteData(researcherRole);
    memoryIndexTable.grantReadWriteData(researcherRole);
    ttlIndexTable.grantWriteData(researcherRole);
    key.grantEncryptDecrypt(researcherRole);

    // --- Auditor: read-only across all three indexes -------------------------
    // No embed-cache access (claude-review S4 — the shared cache crosses trust
    // boundaries). Still needs Bedrock embed to build query vectors for QueryVectors.
    const auditorRole = new iam.Role(this, 'MemoryAuditorRole', {
      roleName: 'MemoryAuditorRole',
      assumedBy: trustPrincipal,
      description: 'Auditor - read-only across shared + all private indexes',
    });
    auditorRole.addToPolicy(vectorActions(RO_VECTORS, [sharedIndex, plannerIndex, researcherIndex]));
    auditorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ContentReadOnly',
        actions: ['s3:GetObject'],
        resources: [`${contentBucket.bucketArn}/*`],
      }),
    );
    auditorRole.addToPolicy(bedrockEmbed());
    memoryIndexTable.grantReadData(auditorRole); // list_memories lookups; no embed cache
    key.grantDecrypt(auditorRole); // read externalized content

    // --- TTL worker (PR 3): lifecycle transitions on all indexes -------------
    // Most destructive role in the system (DeleteVectors) — safety rails live in
    // ttl_worker.py (circuit breaker / DRY_RUN / DLQ, claude-review S6). Content-
    // object cleanup and purge_memory are added in PR 3.
    const ttlRole = new iam.Role(this, 'MemoryTtlRole', {
      roleName: 'MemoryTtlRole',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'TTL worker - supersede/archive/delete lifecycle + index reconciliation',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });
    ttlRole.addToPolicy(vectorActions(TTL_VECTORS, [sharedIndex, plannerIndex, researcherIndex]));
    memoryIndexTable.grantReadWriteData(ttlRole);
    ttlIndexTable.grantReadWriteData(ttlRole);
    ttlRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EmitTtlMetrics',
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'], // PutMetricData does not support resource-level scoping
        conditions: { StringEquals: { 'cloudwatch:namespace': METRIC_NAMESPACE_TTL } },
      }),
    );

    // --- Admin: scoped human maintenance (purge_memory) -----------------------
    // The only HUMAN-assumable role with DeleteVectors. purge_memory (compliance /
    // data-subject deletion, demo cleanup) previously required raw account-admin
    // credentials — CloudTrail then attributed purges to the SSO role, not an agent
    // identity. Assuming this role instead keeps maintenance least-privilege
    // (three indexes + content bucket + the tables, nothing else) and attributed
    // via RoleSessionName=agent_id, mirroring how the TTL worker quarantines
    // automated deletion. Not an agent tool role: create_memory_tools has no
    // 'admin' surface; use `vv purge --role admin` or the client directly.
    const ADMIN_VECTORS = [
      's3vectors:DeleteVectors',
      's3vectors:PutVectors',
      's3vectors:QueryVectors',
      's3vectors:GetVectors',
      's3vectors:ListVectors',
    ];
    const adminRole = new iam.Role(this, 'MemoryAdminRole', {
      roleName: 'MemoryAdminRole',
      assumedBy: trustPrincipal,
      description: 'Admin - scoped maintenance/purge across all indexes (only human-assumable DeleteVectors)',
    });
    adminRole.addToPolicy(vectorActions(ADMIN_VECTORS, [sharedIndex, plannerIndex, researcherIndex]));
    adminRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ContentMaintain', // purge deletes derived content objects in any index prefix
        actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject'],
        resources: [`${contentBucket.bucketArn}/*`],
      }),
    );
    adminRole.addToPolicy(bedrockEmbed()); // retrieve-for-verification before/after a purge
    adminRole.addToPolicy(clientMetrics());
    embedCacheTable.grantReadWriteData(adminRole);
    memoryIndexTable.grantReadWriteData(adminRole); // canonical-row lookup + delete
    ttlIndexTable.grantReadWriteData(adminRole);
    key.grantEncryptDecrypt(adminRole);

    // --- SourceIdentity: permit (and, via trustPrincipal above, require) it ---
    // assumedBy only emits sts:AssumeRole; setting a source identity is a distinct
    // action that each human role must also allow for the trusted principal. Paired
    // with the StringLike sts:SourceIdentity='*' condition on trustPrincipal, this is
    // ENFORCE mode: no source identity -> AssumeRole denied (design-doc §5). Excludes
    // the TTL role — it is assumed by the Lambda service, no human behind it.
    const allowSetSourceIdentity = (role: iam.Role) =>
      role.assumeRolePolicy?.addStatements(
        new iam.PolicyStatement({
          sid: 'AllowSetSourceIdentity',
          actions: ['sts:SetSourceIdentity'],
          principals: [trustPrincipal],
        }),
      );
    [plannerRole, researcherRole, auditorRole, adminRole].forEach(allowSetSourceIdentity);

    // --- TTL Lambda: EventBridge daily -> lifecycle sweep --------------------
    // DRY_RUN defaults to 'true' so the first deploy logs intended actions and
    // deletes nothing (claude-review S6). Enable real deletion with
    // `-c ttlDryRun=false` once a dry run looks right (see below).
    const ttlDlq = new sqs.Queue(this, 'TtlDlq', {
      queueName: TTL_DLQ_NAME,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      retentionPeriod: cdk.Duration.days(14),
    });
    ttlDlq.grantSendMessages(ttlRole);

    // boto3 layer (PR 5 finalization): the Python 3.12 Lambda runtime bundles an older
    // boto3 that may lack the s3vectors client. DRY_RUN=true makes the first deploy a
    // safe no-op, but before flipping DRY_RUN off, publish a boto3>=1.43.31 layer
    // (scripts/build_boto3_layer.sh) and pass its ARN via `-c boto3LayerArn=<arn>`.
    // Kept as a context knob so CI synth needs no Docker/pip bundling.
    const boto3LayerArn: string | undefined = this.node.tryGetContext('boto3LayerArn');
    const ttlLayers = boto3LayerArn
      ? [lambda.LayerVersion.fromLayerVersionArn(this, 'Boto3Layer', boto3LayerArn)]
      : undefined;

    // DRY_RUN safety flag (claude-review S6): default 'true' (log intended actions,
    // delete nothing). Enable real deletion durably with `-c ttlDryRun=false`
    // (also accepts a boolean `false` from cdk.json context). This replaces the
    // one-off `aws lambda update-function-configuration` override so a redeploy
    // no longer silently reverts the setting.
    const dryRunCtx = this.node.tryGetContext('ttlDryRun');
    const ttlDryRun = dryRunCtx === false || dryRunCtx === 'false' ? 'false' : 'true';

    const ttlWorker = new lambda.Function(this, 'TtlWorker', {
      functionName: TTL_WORKER_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'vectorvault.ttl_worker.handler',
      // The vectorvault package is boto3-only at import time (lazy __init__), so the
      // asset carries no third-party deps; the s3vectors-capable boto3 rides in via
      // the optional layer above.
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'src'), {
        exclude: ['**/__pycache__', '**/*.pyc', '**/*.egg-info', '**/*.dist-info'],
      }),
      role: ttlRole,
      layers: ttlLayers,
      timeout: cdk.Duration.minutes(5),
      memorySize: 256,
      deadLetterQueue: ttlDlq,
      environment: {
        VECTOR_BUCKET: VECTOR_BUCKET_NAME,
        INDEXES: [INDEX_NAMES.shared, INDEX_NAMES.planner, INDEX_NAMES.researcher].join(','),
        MEMORY_INDEX_TABLE,
        TTL_INDEX_TABLE,
        DRY_RUN: ttlDryRun,
        MAX_DELETE_ABS: '1000',
        MAX_DELETE_PCT: '0.05',
      },
    });

    new events.Rule(this, 'TtlSchedule', {
      ruleName: 'vectorvault-ttl-daily',
      schedule: events.Schedule.rate(cdk.Duration.days(1)),
      targets: [new targets.LambdaFunction(ttlWorker)],
    });

    // =========================================================================
    // Alerts SNS topic (reused by PR 5 monitoring) + email subscription.
    // =========================================================================
    const alertsTopic = new sns.Topic(this, 'AlertsTopic', {
      topicName: ALERTS_TOPIC_NAME,
      displayName: 'VectorVault alerts',
    });
    alertsTopic.addSubscription(new subscriptions.EmailSubscription(alertEmail));

    // AWS Budgets must be able to publish budget notifications to the topic.
    const alertsTopicPolicy = new sns.TopicPolicy(this, 'AlertsTopicPolicy', {
      topics: [alertsTopic],
    });
    alertsTopicPolicy.document.addStatements(
      new iam.PolicyStatement({
        sid: 'AllowBudgetsPublish',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('budgets.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [alertsTopic.topicArn],
      }),
    );

    // =========================================================================
    // AWS Budget — $20/month hard cap; alarm at 80% ($16). Owner constraint
    // (design-doc §1/§6). ACCOUNT-WIDE by decision
    // (owner, 2026-07-08): account <AWS_ACCOUNT_ID> also hosts llm-forge et al., but
    // those are paused, so total account spend == VectorVault spend for now. If
    // other projects resume, scope this to VectorVault by adding a costFilters on
    // the `project: vectorvault` cost-allocation tag (all resources are tagged;
    // activate the tag in the Billing console first, ~24h to take effect).
    // =========================================================================
    const budget = new budgets.CfnBudget(this, 'MonthlyBudget', {
      budget: {
        // No fixed budgetName: changing NotificationsWithSubscribers (e.g. the
        // alert email) forces CloudFormation to REPLACE the budget, and a fixed
        // name collides with the not-yet-deleted old budget. An auto-generated
        // name lets notification changes deploy cleanly. (Nothing references the
        // name; the budget cap + tags identify it.)
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: budgetUsd, unit: 'USD' },
      },
      // Each notification targets BOTH a direct EMAIL subscriber and the SNS topic.
      // The direct EMAIL is the reliable primary alert: unlike an SNS email
      // subscription, it needs no manual confirmation click and no Region coupling —
      // it always fires. The SNS subscriber gives PR 5 monitoring a fan-out hook.
      notificationsWithSubscribers: [
        {
          // Alarm at 80% of the $20 cap on actual spend.
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: 80,
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [
            { subscriptionType: 'EMAIL', address: alertEmail },
            { subscriptionType: 'SNS', address: alertsTopic.topicArn },
          ],
        },
        {
          // Early warning if the month is forecast to blow the full cap.
          notification: {
            notificationType: 'FORECASTED',
            comparisonOperator: 'GREATER_THAN',
            threshold: 100,
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [
            { subscriptionType: 'EMAIL', address: alertEmail },
            { subscriptionType: 'SNS', address: alertsTopic.topicArn },
          ],
        },
      ],
    });
    budget.node.addDependency(alertsTopicPolicy); // publish perms must exist first

    // =========================================================================
    // CloudTrail — authoritative audit trail (design-doc §5). Write-only data
    // events on the vector bucket (PutVectors/DeleteVectors) keep cost ~$0.30/mo.
    // Raw CfnTrail + explicit log bucket for full control over advanced selectors.
    // =========================================================================
    const trailName = 'vectorvault-audit-trail';
    const trailArn = this.formatArn({
      service: 'cloudtrail',
      resource: 'trail',
      resourceName: trailName,
    });

    const trailLogBucket = new s3.Bucket(this, 'CloudTrailLogsBucket', {
      bucketName: `${TRAIL_LOG_BUCKET_NAME_PREFIX}-${nameSuffix}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED, // SSE-S3: avoids CloudTrail<->KMS coupling
      enforceSSL: true,
      removalPolicy,
      autoDeleteObjects,
      lifecycleRules: [{ expiration: cdk.Duration.days(365) }], // cap log storage cost
    });

    // Canonical CloudTrail bucket policy. Uses a computed trail ARN (not a ref) to
    // avoid a bucket<->trail dependency cycle.
    trailLogBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AWSCloudTrailAclCheck',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('cloudtrail.amazonaws.com')],
        actions: ['s3:GetBucketAcl'],
        resources: [trailLogBucket.bucketArn],
        conditions: { StringEquals: { 'aws:SourceArn': trailArn } },
      }),
    );
    trailLogBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AWSCloudTrailWrite',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('cloudtrail.amazonaws.com')],
        actions: ['s3:PutObject'],
        resources: [`${trailLogBucket.bucketArn}/AWSLogs/${account}/*`],
        conditions: {
          StringEquals: {
            's3:x-amz-acl': 'bucket-owner-full-control',
            'aws:SourceArn': trailArn,
          },
        },
      }),
    );

    const trail = new cloudtrail.CfnTrail(this, 'AuditTrail', {
      trailName,
      s3BucketName: trailLogBucket.bucketName,
      isLogging: true,
      enableLogFileValidation: true,
      includeGlobalServiceEvents: true,
      isMultiRegionTrail: false,
      advancedEventSelectors: [
        {
          name: 'Management events',
          fieldSelectors: [{ field: 'eventCategory', equalTo: ['Management'] }],
        },
        {
          // Write-only S3 Vectors data events on the vector bucket (all its indexes).
          name: 'S3 Vectors write data events',
          fieldSelectors: [
            { field: 'eventCategory', equalTo: ['Data'] },
            { field: 'resources.type', equalTo: ['AWS::S3Vectors::VectorBucket'] },
            { field: 'readOnly', equalTo: ['false'] },
            { field: 'resources.ARN', startsWith: [vectorBucket.attrVectorBucketArn] },
          ],
        },
      ],
    });
    // CloudTrail validates the bucket policy on create — it must be in place first.
    trail.node.addDependency(trailLogBucket.policy!);

    // =========================================================================
    // SSM config contract (claude-review Q5). The Python client (PR 2) reads
    // these instead of hardcoding ARNs.
    // =========================================================================
    const param = (id: string, name: string, value: string) =>
      new ssm.StringParameter(this, id, { parameterName: name, stringValue: value });

    param('ParamRegion', ssmParam('region'), region);
    param('ParamVectorBucketName', ssmParam('vector-bucket-name'), VECTOR_BUCKET_NAME);
    param('ParamVectorBucketArn', ssmParam('vector-bucket-arn'), vectorBucket.attrVectorBucketArn);
    param('ParamContentBucketName', ssmParam('content-bucket-name'), contentBucket.bucketName);

    param('ParamSharedIndexName', ssmParam('index', 'shared-team-memory-name'), INDEX_NAMES.shared);
    param('ParamSharedIndexArn', ssmParam('index', 'shared-team-memory-arn'), sharedIndex.attrIndexArn);
    param('ParamPlannerIndexName', ssmParam('index', 'private-planner-name'), INDEX_NAMES.planner);
    param('ParamPlannerIndexArn', ssmParam('index', 'private-planner-arn'), plannerIndex.attrIndexArn);
    param('ParamResearcherIndexName', ssmParam('index', 'private-researcher-name'), INDEX_NAMES.researcher);
    param('ParamResearcherIndexArn', ssmParam('index', 'private-researcher-arn'), researcherIndex.attrIndexArn);

    param('ParamEmbedCacheTable', ssmParam('table', 'memory-embed-cache'), embedCacheTable.tableName);
    param('ParamMemoryIndexTable', ssmParam('table', 'memory-index'), memoryIndexTable.tableName);
    param('ParamMemoryIndexGsi', ssmParam('table', 'memory-index-task-gsi'), MEMORY_INDEX_TASK_GSI);
    param('ParamTtlIndexTable', ssmParam('table', 'memory-ttl-index'), ttlIndexTable.tableName);

    param('ParamEmbedModelId', ssmParam('embed-model-id'), EMBED_MODEL_ID);
    param('ParamKmsKeyArn', ssmParam('kms-key-arn'), key.keyArn);
    param('ParamAlertsTopicArn', ssmParam('alerts-topic-arn'), alertsTopic.topicArn);

    param('ParamPlannerRoleArn', ssmParam('role', 'planner-arn'), plannerRole.roleArn);
    param('ParamResearcherRoleArn', ssmParam('role', 'researcher-arn'), researcherRole.roleArn);
    param('ParamAuditorRoleArn', ssmParam('role', 'auditor-arn'), auditorRole.roleArn);
    param('ParamAdminRoleArn', ssmParam('role', 'admin-arn'), adminRole.roleArn);
    param('ParamTtlRoleArn', ssmParam('role', 'ttl-arn'), ttlRole.roleArn);

    // =========================================================================
    // CloudFormation outputs — quick-reference after deploy.
    // =========================================================================
    new cdk.CfnOutput(this, 'VectorBucketArn', { value: vectorBucket.attrVectorBucketArn });
    new cdk.CfnOutput(this, 'ContentBucketName', { value: contentBucket.bucketName });
    new cdk.CfnOutput(this, 'SharedIndexArn', { value: sharedIndex.attrIndexArn });
    new cdk.CfnOutput(this, 'MemoryIndexTableName', { value: memoryIndexTable.tableName });
    new cdk.CfnOutput(this, 'AlertsTopicArn', { value: alertsTopic.topicArn });

    // Tag every taggable resource for Cost Explorer verification of the $20 cap.
    cdk.Tags.of(this).add(PROJECT_TAG_KEY, PROJECT_TAG_VALUE);
  }
}
