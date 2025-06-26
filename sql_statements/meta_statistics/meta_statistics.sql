/*
SPDX-FileCopyrightText: 2023 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
*/

CREATE SCHEMA IF NOT EXISTS statistics;

/* Create 'readonly' role and grant SELECT on tables in the 'statistics' schema 
CREATE ROLE readonly;
GRANT CONNECT ON DATABASE postgres TO readonly;
GRANT USAGE ON SCHEMA statistics TO readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA statistics TO readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA statistics GRANT SELECT ON TABLES to readonly;
*/

/* Waiting connections statistics 

Original query:
     SELECT (NOW() at time zone 'utc') AS time,
            COUNT(*) AS waiting_connections
       FROM pg_stat_activity
      WHERE wait_event_type = 'Lock';

*/
CREATE TABLE IF NOT EXISTS statistics.waiting_connections (
    time TIMESTAMP,
    waiting_connections INTEGER
);

CREATE INDEX IF NOT EXISTS idx_waiting_connections_time
ON statistics.waiting_connections(time);

CREATE OR REPLACE PROCEDURE log_waiting_connections()
LANGUAGE plpgsql
AS $$
DECLARE
    waiting_count INT;
BEGIN
    -- Get the count of waiting connections
    SELECT COUNT(*) INTO waiting_count
    FROM pg_stat_activity
    WHERE wait_event_type = 'Lock';

    -- Insert only if waiting_count is greater than 0
    IF waiting_count > 0 THEN
        INSERT INTO statistics.waiting_connections (time, waiting_connections)
        VALUES ((NOW() at time zone 'utc'), waiting_count);
    END IF;
END;
$$;

CALL schedule_cron_job('0-59 * * * *', 'log_waiting_connections');


/* Bucketed query statistics
Original query:
SELECT (NOW() at time zone 'utc') AS time,
       state,
       query_duration_bucket,
       MAX(long_running_queries) AS long_running_queries
FROM (
       SELECT CASE
                WHEN (clock_timestamp() - query_start) < interval '2 seconds'
                     THEN '1-2 seconds'
                WHEN (clock_timestamp() - query_start) < interval '5 seconds'
                     THEN '2-5 seconds'
                WHEN (clock_timestamp() - query_start) < interval '10 seconds'
                     THEN '5-10 seconds'
                WHEN (clock_timestamp() - query_start) < interval '30 seconds'
                     THEN '10-30 seconds'
                WHEN (clock_timestamp() - query_start) < interval '60 seconds'
                     THEN '30-60 seconds'
                WHEN (clock_timestamp() - query_start) < interval '300 seconds'
                     THEN '60-300 seconds'
                ELSE '300+ seconds'
              END AS query_duration_bucket,
              COUNT(*) AS long_running_queries,
              state
       FROM pg_stat_activity
       WHERE (clock_timestamp() - query_start) > interval '1 second'
       GROUP BY query_duration_bucket, state
     ) AS query_duration_bucket
WHERE long_running_queries > 0
GROUP BY query_duration_bucket, state
ORDER BY state,
         CASE query_duration_bucket
           WHEN '1-2 seconds' THEN 1
           WHEN '2-5 seconds' THEN 2
           WHEN '5-10 seconds' THEN 3
           WHEN '10-30 seconds' THEN 4
           WHEN '30-60 seconds' THEN 5
           WHEN '60-300 seconds' THEN 6
           WHEN '300+ seconds' THEN 7
         END;

*/

CREATE TABLE IF NOT EXISTS statistics.bucketed_query_statistics (
    time TIMESTAMP,
    state TEXT,
    query_duration_bucket TEXT,
    long_running_queries INTEGER
);

CREATE INDEX IF NOT EXISTS idx_bucketed_query_statistics_time 
ON statistics.bucketed_query_statistics(time);

CREATE INDEX IF NOT EXISTS idx_bucketed_query_statistics_time_state 
ON statistics.bucketed_query_statistics(time, state);

CREATE INDEX IF NOT EXISTS idx_bucketed_query_statistics_duration_bucket 
ON statistics.bucketed_query_statistics(query_duration_bucket);

CREATE INDEX IF NOT EXISTS idx_bucketed_query_statistics_duration_bucket_state
ON statistics.bucketed_query_statistics(query_duration_bucket, state);

CREATE OR REPLACE PROCEDURE log_bucketed_query_statistics()
LANGUAGE plpgsql
AS $$
BEGIN
     INSERT INTO statistics.bucketed_query_statistics (time, state, query_duration_bucket, long_running_queries)
     SELECT (NOW() at time zone 'utc') AS time,
             state,
             query_duration_bucket,
             MAX(long_running_queries) AS long_running_queries
     FROM (
             SELECT CASE
                         WHEN (clock_timestamp() - query_start) < interval '2 seconds'
                          THEN '1-2 seconds'
                         WHEN (clock_timestamp() - query_start) < interval '5 seconds'
                          THEN '2-5 seconds'
                         WHEN (clock_timestamp() - query_start) < interval '10 seconds'
                          THEN '5-10 seconds'
                         WHEN (clock_timestamp() - query_start) < interval '30 seconds'
                          THEN '10-30 seconds'
                         WHEN (clock_timestamp() - query_start) < interval '60 seconds'
                          THEN '30-60 seconds'
                         WHEN (clock_timestamp() - query_start) < interval '300 seconds'
                          THEN '60-300 seconds'
                         ELSE '300+ seconds'
                      END AS query_duration_bucket,
                      COUNT(*) AS long_running_queries,
                      state
             FROM pg_stat_activity
             WHERE (clock_timestamp() - query_start) > interval '1 second'
             GROUP BY query_duration_bucket, state
           ) AS query_duration_bucket
     WHERE long_running_queries > 0
     GROUP BY query_duration_bucket, state
     ORDER BY state,
                CASE query_duration_bucket
                  WHEN '1-2 seconds' THEN 1
                  WHEN '2-5 seconds' THEN 2
                  WHEN '5-10 seconds' THEN 3
                  WHEN '10-30 seconds' THEN 4
                  WHEN '30-60 seconds' THEN 5
                  WHEN '60-300 seconds' THEN 6
                  WHEN '300+ seconds' THEN 7
                END;
     END;
$$;

CALL schedule_cron_job('0-59 * * * *', 'log_bucketed_query_statistics');

/* Lock conflicts statistics
Original query:
     SELECT (NOW() at time zone 'utc') AS time,
            COUNT(*) AS lock_conflicts
       FROM pg_stat_activity t1
       JOIN pg_locks l1 ON t1.pid = l1.pid
       JOIN pg_locks l2 ON l1.locktype = l2.locktype
        AND l1.DATABASE IS NOT DISTINCT FROM l2.DATABASE
        AND l1.relation IS NOT DISTINCT FROM l2.relation
      WHERE t1.state = 'active'
        AND l1.granted IS NOT true;

*/
CREATE TABLE IF NOT EXISTS statistics.lock_conflicts (
    time TIMESTAMP,
    lock_conflicts INTEGER
);

CREATE INDEX IF NOT EXISTS idx_lock_conflicts_time
ON statistics.lock_conflicts(time);

CREATE OR REPLACE PROCEDURE log_lock_conflicts()
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO statistics.lock_conflicts (time, lock_conflicts)
    SELECT (NOW() at time zone 'utc') AS time,
           COUNT(*) AS lock_conflicts
    FROM pg_stat_activity t1
    JOIN pg_locks l1 ON t1.pid = l1.pid
    JOIN pg_locks l2 ON l1.locktype = l2.locktype
    AND l1.DATABASE IS NOT DISTINCT FROM l2.DATABASE
    AND l1.relation IS NOT DISTINCT FROM l2.relation
    WHERE t1.state = 'active'
    AND l1.granted IS NOT true;
END;
$$;

CALL schedule_cron_job('0-59 * * * *', 'log_lock_conflicts');

/* Dead row statistics
Original query:
       SELECT (NOW() at time zone 'utc') AS time,
            SUM(COALESCE(n_dead_tup,0)) AS dead_rows
       FROM pg_stat_user_tables;
*/
CREATE TABLE IF NOT EXISTS statistics.dead_rows (
    time TIMESTAMP,
    dead_rows INTEGER
);

CREATE INDEX IF NOT EXISTS idx_dead_rows_time
ON statistics.dead_rows(time);

CREATE OR REPLACE PROCEDURE log_dead_rows()
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO statistics.dead_rows (time, dead_rows)
    SELECT (NOW() at time zone 'utc') AS time,
           SUM(COALESCE(n_dead_tup,0)) AS dead_rows
    FROM pg_stat_user_tables;
END;
$$;

CALL schedule_cron_job('0-59 * * * *', 'log_dead_rows');

UPDATE cron.job SET nodename = '';


/* Any-state long-running-queries count statistics
Original query:
     SELECT COUNT(*)
          FROM pg_stat_activity
          WHERE 
               state IS NOT NULL
               AND query NOT LIKE '%ROLLBACK%'
               AND query NOT LIKE '%COMMIT%'
               AND (now() - query_start) > interval '5 seconds';
*/
CREATE TABLE IF NOT EXISTS statistics.any_state_long_running_queries (
    time TIMESTAMP,
    long_running_queries INTEGER
);

CREATE INDEX IF NOT EXISTS idx_any_state_long_running_queries_time
ON statistics.any_state_long_running_queries(time);

CREATE OR REPLACE PROCEDURE log_any_state_long_running_queries()
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO statistics.any_state_long_running_queries (time, long_running_queries)
    SELECT (NOW() at time zone 'utc') AS time,
           COUNT(*)
    FROM pg_stat_activity
    WHERE 
         state IS NOT NULL
         AND query NOT LIKE '%ROLLBACK%'
         AND query NOT LIKE '%COMMIT%'
         AND (now() - query_start) > interval '5 seconds';
END;

$$;

CALL schedule_cron_job('0-59 * * * *', 'log_any_state_long_running_queries');
