import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { asyncBufferFromFile, parquetMetadataAsync } from 'hyparquet';
import { epoch, openDataset, planGroups, validate } from './dashboard-data.ts';

const read = async (name: string) => parquetMetadataAsync(await asyncBufferFromFile(`public/data/${name}`));

test('validates fixture and rejects incompatible version and physical types', async () => {
  const good = await read('one-commit.parquet');
  assert.equal(validate(good).panels.size, 7);
  const optional = structuredClone(good);
  optional.schema.find(f => f.name === 'window_functions')!.type = 'DOUBLE';
  assert.equal(validate(optional).panels.has('operator'), false);
  const invalid = await read('incompatible.parquet');
  assert.throws(() => validate(invalid), /expected Parquet contract v1/);
  good.schema.find(f => f.name === 'duration_s')!.type = 'INT32';
  assert.throws(() => validate(good), /engine_batch.duration_s must be DOUBLE/);
});

test('prunes by section and commit date but keeps groups without usable statistics', async () => {
  const meta = await read('_server.parquet');
  const bounds = planGroups(meta, 'engine_batch', { since: Date.UTC(2026, 10, 12) });
  const runRows = validate(meta).counts.get('run');
  assert.deepEqual(bounds, [{ start: runRows, end: runRows! + 24 * 24 }]);
  assert.equal(planGroups(meta, 'model', { at: Date.UTC(2025, 0, 1) }).length, 0);
  const section = meta.row_groups[0].columns.find(c => c.meta_data?.path_in_schema[0] === 'section')!;
  section.meta_data!.statistics = undefined;
  assert.equal(planGroups(meta, 'model')[0].start, 0);
  assert.equal(epoch(new Date(12345)), epoch(12345n));
  assert.ok(Number.isNaN(epoch(2n ** 55n)));
});

test('reads snapshot sections for merging and rejects incompatible snapshots', async () => {
  const file = new File([await readFile('public/data/one-commit.parquet')], 'one-commit.parquet');
  const snapshot = await openDataset(file);
  assert.equal((await snapshot.read('run')).length, 1);
  assert.equal((await snapshot.read('engine_batch')).length, 24);
  assert.equal((await snapshot.read('model')).length, snapshot.report.counts.get('model'));
  const incompatible = new File([await readFile('public/data/incompatible.parquet')], 'incompatible.parquet');
  await assert.rejects(openDataset(incompatible), /expected Parquet contract v1/);
});
