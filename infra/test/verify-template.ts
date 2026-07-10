/**
 * Synth-level verification of PR 1's one-way-door and least-privilege config.
 *
 * These assertions lock the immutable-after-creation settings (design-doc §5,
 * implementation-plan.md PR 1 checklist) so a future edit can't silently regress
 * them. This runs in CI (`npm test`) at synth time — it does NOT replace the live
 * post-deploy check (`aws s3vectors get-index`), which still runs after `cdk deploy`.
 *
 * Run: `npm test` (ts-node). Throws + exits non-zero on any mismatch.
 */
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { MemoryStack } from '../lib/memory-stack';
import { INDEX_NAMES, NON_FILTERABLE_METADATA_KEYS } from '../lib/config';

const app = new cdk.App();
const stack = new MemoryStack(app, 'VerifyStack');
const t = Template.fromStack(stack);

const checks: Array<[string, () => void]> = [
  // --- One-way door: vector-bucket SSE-KMS with the FULL key ARN --------------
  ['vector bucket encrypted with full KMS ARN (aws:kms)', () =>
    t.hasResourceProperties('AWS::S3Vectors::VectorBucket', {
      EncryptionConfiguration: {
        SseType: 'aws:kms',
        KmsKeyArn: { 'Fn::GetAtt': Match.arrayWith([Match.stringLikeRegexp('MemoryKey.*'), 'Arn']) },
      },
    })],

  // --- One-way door: exactly 3 indexes, each with the final schema ------------
  ['exactly three vector indexes', () => t.resourceCountIs('AWS::S3Vectors::Index', 3)],
  ...Object.values(INDEX_NAMES).map((name): [string, () => void] => [
    `index ${name}: 1024/cosine/float32 + final non-filterable keys`, () =>
      t.hasResourceProperties('AWS::S3Vectors::Index', {
        IndexName: name,
        DataType: 'float32',
        Dimension: 1024,
        DistanceMetric: 'cosine',
        MetadataConfiguration: { NonFilterableMetadataKeys: NON_FILTERABLE_METADATA_KEYS },
      }),
  ]),

  // --- One-way door: KMS key policy grants decrypt to the indexing service ----
  ['KMS key grants kms:Decrypt to indexing.s3vectors.amazonaws.com', () =>
    t.hasResourceProperties('AWS::KMS::Key', {
      KeyPolicy: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: { Service: 'indexing.s3vectors.amazonaws.com' },
            Action: Match.arrayWith(['kms:Decrypt']),
          }),
        ]),
      }),
    })],

  // --- CloudTrail write-only S3 Vectors data events on the vector bucket ------
  ['CloudTrail logs write-only S3 Vectors data events', () =>
    t.hasResourceProperties('AWS::CloudTrail::Trail', {
      AdvancedEventSelectors: Match.arrayWith([
        Match.objectLike({
          FieldSelectors: Match.arrayWith([
            Match.objectLike({ Field: 'resources.type', Equals: ['AWS::S3Vectors::VectorBucket'] }),
            Match.objectLike({ Field: 'readOnly', Equals: ['false'] }),
          ]),
        }),
      ]),
    })],

  // --- Cost cap: $20 monthly budget ------------------------------------------
  ['$20/month budget cap', () =>
    t.hasResourceProperties('AWS::Budgets::Budget', {
      Budget: Match.objectLike({ BudgetLimit: { Amount: 20, Unit: 'USD' } }),
    })],

  // --- Least privilege: DeleteVectors exists on exactly two policies ----------
  // (the TTL worker's automated lifecycle + the human-assumable MemoryAdminRole
  // for attributed purge_memory). Agent roles (planner/researcher/auditor) must
  // never gain it — retraction is archive_memory, a reversible metadata rewrite.
  ['DeleteVectors granted to exactly two roles (TTL worker + admin), no agent roles', () => {
    const policies = t.findResources('AWS::IAM::Policy');
    const withDelete = Object.values(policies).filter((p) =>
      JSON.stringify(p.Properties?.PolicyDocument ?? {}).includes('s3vectors:DeleteVectors'),
    );
    if (withDelete.length !== 2) {
      throw new Error(`expected DeleteVectors on 2 policies, found ${withDelete.length}`);
    }
    const attachedRoles = withDelete.flatMap((p) =>
      (p.Properties?.Roles ?? []).map((r: unknown) => JSON.stringify(r)),
    );
    const allowed = /MemoryTtlRole|MemoryAdminRole/;
    const strays = attachedRoles.filter((r) => !allowed.test(r));
    if (strays.length > 0) {
      throw new Error(`DeleteVectors attached outside TTL/Admin: ${strays.join(', ')}`);
    }
  }],

  // --- Recovery levers: PITR on all DynamoDB tables (embed-cache, index, ttl) -
  ['all three DynamoDB tables have PITR enabled', () =>
    t.resourcePropertiesCountIs(
      'AWS::DynamoDB::Table',
      { PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true } },
      3,
    )],

  // --- TTL worker (PR 3): Lambda ships DRY_RUN on + has a DLQ ------------------
  ['TTL Lambda defaults DRY_RUN=true with a dead-letter queue', () =>
    t.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'vectorvault.ttl_worker.handler',
      Environment: { Variables: Match.objectLike({ DRY_RUN: 'true' }) },
      DeadLetterConfig: Match.objectLike({ TargetArn: Match.anyValue() }),
    })],

  ['TTL worker runs on an EventBridge schedule', () =>
    t.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: Match.stringLikeRegexp('rate\\(1 day'),
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
  console.error(`\ntemplate verification FAILED: ${failed} check(s)`);
  process.exit(1);
}
console.log(`\ntemplate verification OK: ${checks.length} checks passed`);
