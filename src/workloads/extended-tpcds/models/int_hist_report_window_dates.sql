SELECT
  ds.report_as_of_date,
  lag.n AS window_lag_days,
  TRY_CAST(ds.report_as_of_date AS DATE) + INTERVAL (-lag.n) DAY AS effective_date
FROM int_hist_date_spine AS ds
-- Both parity keys carry the complete lag list, so every date row matches
-- exactly 28 rows. NULL dates use key 0 and retain all 28 rows as well.
INNER JOIN (VALUES
    (0, 0), (1, 0),
    (0, 1), (1, 1),
    (0, 2), (1, 2),
    (0, 3), (1, 3),
    (0, 4), (1, 4),
    (0, 5), (1, 5),
    (0, 6), (1, 6),
    (0, 7), (1, 7),
    (0, 8), (1, 8),
    (0, 9), (1, 9),
    (0, 10), (1, 10),
    (0, 11), (1, 11),
    (0, 12), (1, 12),
    (0, 13), (1, 13),
    (0, 14), (1, 14),
    (0, 15), (1, 15),
    (0, 16), (1, 16),
    (0, 17), (1, 17),
    (0, 18), (1, 18),
    (0, 19), (1, 19),
    (0, 20), (1, 20),
    (0, 21), (1, 21),
    (0, 22), (1, 22),
    (0, 23), (1, 23),
    (0, 24), (1, 24),
    (0, 25), (1, 25),
    (0, 26), (1, 26),
    (0, 27), (1, 27)
) AS lag(date_parity, n)
  ON COALESCE(CAST(EXTRACT(DAY FROM ds.report_as_of_date) AS INT), 0) % 2 = lag.date_parity
