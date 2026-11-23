import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock as Mock, patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0,str(Path(__file__).parents[1]))
os.environ['EXTENDED_TPCDS_WORKLOAD_DIR']=str(Path(__file__).parents[3]/'workloads/extended-tpcds')
from services import extended_tpcds_postgres as pg


class PostgresWorkloadTests(unittest.TestCase):
    def test_validation_detects_a_missing_duplicate_using_duckdb_arrow_schema(self):
        from services.extended_tpcds import mutations, pipeline
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'plan_id': 'test', 'batches': []}))
            (root / 'pg-checkpoint.json').write_text(json.dumps({
                'dirty': False, 'batch_num': 1, 'plan_id': 'test', 'schema': 'xt_test',
            }))
            connection = Mock()
            cursor = connection.cursor.return_value.__enter__.return_value
            cursor.description = [('value',)]
            cursor.fetchmany.side_effect = [[(1,), (None,)], []]

            def build_reference(reference, **_):
                reference.execute('CREATE TABLE sample AS SELECT * FROM (VALUES (1), (1), (NULL)) AS t(value)')

            with patch.object(pg, 'connect') as connect, patch.object(mutations, 'load_initial'), \
                 patch.object(pipeline, 'build', side_effect=build_reference), \
                 patch.object(pipeline, 'selected_models', return_value=['sample']):
                connect.return_value.__enter__.return_value = connection
                result = pg.validate_batch('materialize-emulator', 1, manifest, root, {'query_cohort': 'all'})
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['differences'], {'sample': 1})

    def test_report_window_equality_join_preserves_cross_join_multiplicity(self):
        from services.extended_tpcds import pipeline
        from services.extended_tpcds_sql import translate
        from services.extended_tpcds_streaming import render as render_streaming

        query = pipeline.render_model('int_hist_report_window_dates', {
            'report_date': '2002-12-31', 'history_months': 12,
        })
        oracle = """SELECT report_as_of_date, n,
                           report_as_of_date + INTERVAL (-n) DAY
                    FROM int_hist_date_spine CROSS JOIN range(28) AS lag(n)"""
        queries = {'duckdb': query, 'dbspnet': render_streaming(query, 'dbspnet'),
                   'feldera': render_streaming(query, 'feldera').replace('SAFE_CAST(', 'TRY_CAST('),
                   'spark': pg.sqlglot.transpile(translate(query), read='spark', write='duckdb')[0]}
        queries.update({engine: pg.render(query, engine) for engine in pg.ENGINES})

        with duckdb.connect() as db:
            db.execute("""CREATE TABLE int_hist_date_spine AS SELECT * FROM
                        (VALUES (DATE '2000-03-01'), (DATE '2000-03-01'),
                                (DATE '2000-03-02'), (NULL)) AS d(report_as_of_date)""")
            db.execute('CREATE TABLE __xt_unit AS SELECT 1 AS one')
            expected = db.execute(oracle).fetchall()
            self.assertEqual(len(expected), 4 * 28)
            for engine, sql in queries.items():
                with self.subTest(engine=engine):
                    join, = pg.sqlglot.parse_one(sql, read='duckdb').find_all(pg.exp.Join)
                    self.assertIsInstance(join.args['on'], pg.exp.EQ)
                    self.assertCountEqual(db.execute(sql).fetchall(), expected)

    def test_all_96_models_render_without_dropping_safe_casts_or_null_semantics(self):
        from services.extended_tpcds import pipeline
        for engine in pg.ENGINES:
            queries={n:pg.render(pipeline.render_model(n,{'report_date':'2002-12-31','history_months':12},'all'),engine)
                     for n in pipeline.selected_models()}
            self.assertEqual(len(queries),96)
            if engine == 'risingwave':
                self.assertTrue(all('TRY_CAST(' not in sql for sql in queries.values()))
            else:
                self.assertIn('TRY_CAST(',queries['meta_ingestion'])
            self.assertTrue(all('INTERVAL DAY' not in sql and 'INTERVAL MONTH' not in sql
                                for sql in queries.values()))
            self.assertTrue(all('TO_TIMESTAMP(' not in sql.upper() for sql in queries.values()))
            effective_units = queries['int_engagement_effective_units']
            self.assertIn('CAST(NULL AS INT) AS "receipt_reported_units"', effective_units)
            self.assertIn('CAST(NULL AS BIGINT) AS "customer_reported_capacity"', effective_units)
            self.assertNotIn('IGNORE NULLS',queries['int_channel_status_latest'])
            self.assertIn('IS NULL',queries['int_channel_status_latest'])

    def test_effective_units_union_uses_consistent_input_types(self):
        from services.extended_tpcds import pipeline

        query = pipeline.render_model('int_engagement_effective_units', {
            'report_date': '2002-12-31', 'history_months': 12,
        })
        with duckdb.connect() as db:
            db.execute("""
                CREATE TABLE int_engagement_observation_latest
                    (engagement_path VARCHAR, customer_path VARCHAR, engagement_reported_units INT);
                CREATE TABLE int_engagement_primary_units
                    (engagement_path VARCHAR, customer_path VARCHAR,
                     engagement_reported_units BIGINT, customer_reported_capacity BIGINT);
                CREATE TABLE int_engagement_receipt_latest
                    (receipt_time TIMESTAMP, engagement_path VARCHAR,
                     customer_path VARCHAR, receipt_reported_units INT);
            """)
            for engine in pg.ENGINES:
                with self.subTest(engine=engine):
                    rendered = pg.sqlglot.parse_one(pg.render(query, engine), read='postgres')
                    silver, receipt, union = rendered.ctes[:3]
                    ctes = pg.exp.With(expressions=[silver.copy(), receipt.copy()])
                    for arm in union.find_all(pg.exp.Select):
                        sql = arm.copy()
                        sql.set('with_', ctes.copy())
                        fields = db.execute(f"DESCRIBE {sql.sql(dialect='duckdb')}").fetchall()
                        self.assertEqual([row[1] for row in fields],
                                         ['VARCHAR', 'VARCHAR', 'BIGINT', 'BIGINT', 'INTEGER'])

    def test_risingwave_prunes_only_the_inactive_cohort_membership(self):
        from services.extended_tpcds import pipeline
        config = {'report_date': '2002-12-31', 'history_months': 12}
        for cohort in ('all', 'hot', 'cold'):
            query = pipeline.render_model('int_target_engagements', config, cohort)
            sql = pg.render(query, 'risingwave')
            membership = [expr for expr in pg.sqlglot.parse_one(sql, read='postgres').find_all(pg.exp.In)
                          if expr.args.get('query')]
            self.assertEqual(len(membership), 0 if cohort == 'all' else 1)

    def test_window_rewrite_preserves_nulls_duplicates_and_whole_partition_results(self):
        source='SELECT k,t,v,FIRST_VALUE(v IGNORE NULLS) OVER (PARTITION BY k ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS first FROM input'
        with duckdb.connect() as db:
            db.execute('CREATE TABLE input(k INT,t INT,v VARCHAR)')
            db.execute("INSERT INTO input VALUES (1,1,NULL),(1,2,'a'),(1,2,'a'),(2,1,NULL),(NULL,1,'b')")
            expected=db.execute(source).fetchall()
            for engine in pg.ENGINES:
                rendered=pg.render(source,engine)
                lowered=pg.sqlglot.transpile(rendered,read='postgres',write='duckdb')[0]
                self.assertCountEqual(db.execute(lowered).fetchall(),expected)
        with self.assertRaisesRegex(ValueError,'whole-partition'):
            pg.render('SELECT FIRST_VALUE(v IGNORE NULLS) OVER (ORDER BY t) FROM input','materialize-emulator')

    def test_nullable_casts_dates_and_json_keep_the_reference_results(self):
        source="SELECT TRY_CAST(dim_date_id AS INT),CAST(TRY_STRPTIME(d,'%Y%m%d') AS DATE),json_extract_string(j,'$.x') FROM input"
        with duckdb.connect() as db:
            db.execute('CREATE TABLE input(dim_date_id VARCHAR,d VARCHAR,j VARCHAR)')
            db.execute("INSERT INTO input VALUES ('12','20021231','{\"x\":\"café\"}'),('invalid','20020230','{}'),(NULL,'01',NULL),('2',NULL,'{\"x\":null}'),('2147483648','20000229',NULL),('-2147483648','19000229',NULL)")
            expected=db.execute(source).fetchall()
            for engine in pg.ENGINES:
                lowered=pg.sqlglot.transpile(pg.render(source,engine),read='postgres',write='duckdb')[0]
                self.assertCountEqual(db.execute(lowered).fetchall(),expected)

    def test_expression_intervals_keep_sign_units_and_parentheses(self):
        query = "SELECT DATE '2002-12-31' + INTERVAL (-(n + 27)) DAY, DATE '2002-12-31' + INTERVAL (-n) MONTH FROM (VALUES (1),(2),(NULL)) AS t(n)"
        with duckdb.connect() as db:
            expected = db.execute(query).fetchall()
            for engine in pg.ENGINES:
                sql = pg.render(query, engine)
                self.assertIn("INTERVAL '1 DAY'", sql)
                self.assertIn("INTERVAL '1 MONTH'", sql)
                self.assertEqual(db.execute(sql).fetchall(), expected)

    def test_strptime_event_keys_remain_unzoned_and_support_second_offsets(self):
        query = """SELECT STRPTIME(k, '%Y%m%d') + seconds * INTERVAL '1' SECOND
                   FROM (VALUES ('20000229', 3605), ('20021231', 86399), (NULL, 1)) AS t(k, seconds)"""
        with duckdb.connect() as db:
            expected = db.execute(query).fetchall()
            for engine in pg.ENGINES:
                sql = pg.render(query, engine)
                self.assertNotIn('TO_TIMESTAMP(', sql.upper())
                self.assertEqual(db.execute(sql).fetchall(), expected)
                self.assertEqual(db.execute(f'SELECT typeof(ts) FROM ({sql}) AS result(ts) LIMIT 1').fetchone(),
                                 ('TIMESTAMP',))

    def test_risingwave_global_window_uses_one_partition_and_preserves_grouped_window(self):
        query = """SELECT k, v, ROW_NUMBER() OVER (ORDER BY v DESC, k) AS global_rank,
                          ROW_NUMBER() OVER (PARTITION BY k ORDER BY v DESC) AS group_rank
                   FROM (VALUES (1, 8), (1, 3), (2, 7), (2, NULL)) AS t(k,v)"""
        sql = pg.render(query, 'risingwave')
        self.assertIn('PARTITION BY CAST(1 AS INT)', sql)
        self.assertIn('PARTITION BY "k"', sql)
        with duckdb.connect() as db:
            self.assertCountEqual(db.execute(sql).fetchall(), db.execute(query).fetchall())

    def test_copy_is_bounded_and_preserves_uuid_nulls_and_escaped_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'rows.parquet';token=uuid.uuid4()
            pq.write_table(pa.table({'id':[str(token),str(token)+'b'],'v':['a\tb\nc\\d',None]}),path)
            connection=Mock();cursor=connection.cursor.return_value.__enter__.return_value
            chunks=[]
            cursor.copy_expert.side_effect=lambda statement,stream:chunks.append(stream.read())
            self.assertEqual(pg.load_image(connection,path,'events',{'id':'UUID','v':'VARCHAR'},'materialize-emulator'),2)
            data=b''.join(chunks).decode()
            self.assertIn('a\\tb\\nc\\\\d',data)
            self.assertIn('\\N',data)
            self.assertIn(str(token),data)

    def test_risingwave_inserts_use_bounded_values_without_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'rows.parquet';pq.write_table(pa.table({'id':range(300)}),path)
            connection=Mock()
            with patch.object(pg,'execute_values') as execute:
                self.assertEqual(pg.load_image(connection,path,'events',{'id':'BIGINT'},'risingwave'),300)
            self.assertEqual(execute.call_args.kwargs['page_size'],128)
            self.assertEqual(len(execute.call_args.args[2]),300)

    def test_source_checksum_and_unsupported_mutations_fail_before_writes(self):
        from services.extended_tpcds import mutations
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/'rows.parquet';pq.write_table(pa.table({'id':[1]}),p)
            image={'path':p.name,'rows':1,'sha256':'wrong'}
            manifest={'schemas':{'events':{'id':'BIGINT'}},'initial':{'events':image},
                      'batches':[{'operations':[{'kind':'delete','table':'events','before':image,'after':None}]}]}
            connection=Mock()
            with self.assertRaisesRegex(ValueError,'checksum'):
                pg.replay(connection,manifest,root,1,'risingwave')
            with self.assertRaisesRegex(ValueError,'insert-only'):
                pg.replay(connection,manifest,root,2,'risingwave')
            connection.cursor.assert_not_called()

    def test_connection_closes_even_when_session_setup_fails(self):
        with patch.object(pg.psycopg2,'connect') as factory:
            factory.return_value.cursor.return_value.__enter__.return_value.execute.side_effect=RuntimeError('setup failed')
            with self.assertRaisesRegex(RuntimeError,'setup failed'):
                with pg.connect('risingwave',100):pass
            factory.return_value.close.assert_called_once()

    def test_broken_or_repeated_checkpoint_is_rejected_before_connecting(self):
        from services.extended_tpcds import pipeline
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/'manifest.json'
            p.write_text(json.dumps({'plan_id':'p','source_configuration':{}}))
            (root/'pg-checkpoint.json').write_text(json.dumps({'batch_num':1,'dirty':True,'plan_id':'p'}))
            with patch.object(pipeline,'selected_models',return_value=[]),patch.object(pg,'connect') as connect:
                with self.assertRaisesRegex(ValueError,'checkpoint'):
                    pg.run_batch('risingwave',2,p,root,{'query_cohort':'all'})
                connect.assert_not_called()

    def test_rejected_model_keeps_dirty_checkpoint_and_never_becomes_a_table(self):
        from services.extended_tpcds import pipeline
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/'manifest.json'
            p.write_text(json.dumps({'plan_id':'p','source_configuration':{},'schemas':{},'initial':{}}))
            connection=Mock();cursor=connection.cursor.return_value.__enter__.return_value
            def execute(sql):
                if sql.startswith('CREATE MATERIALIZED VIEW'):raise RuntimeError('unsupported window')
            cursor.execute.side_effect=execute
            with patch.object(pg,'connect') as connect,patch.object(pipeline,'selected_models',return_value=['m']), \
                 patch.object(pipeline,'render_model',return_value='SELECT 1 AS x'):
                connect.return_value.__enter__.return_value=connection
                with self.assertRaisesRegex(RuntimeError,'unsupported window'):
                    pg.run_batch('materialize-emulator',1,p,root,{'query_cohort':'all','statement_timeout_s':300})
            self.assertTrue(json.loads((root/'pg-checkpoint.json').read_text())['dirty'])
            self.assertEqual(json.loads((root/'batch1-failure.json').read_text())['model'],'m')
            self.assertFalse(any(call.args[0].startswith('CREATE TABLE') for call in cursor.execute.call_args_list))
