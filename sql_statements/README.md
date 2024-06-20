## AI-Horde Database Information

- postgresql >=15
- [pg_cron](https://github.com/citusdata/pg_cron)


## `pg_cron` config

> **Warning**: All `.sql` files found in a directory deeper than `sql_statements/` will be dynamically run, not only the ones specifically identified in this document. Only place `.sql` files you intend to run in these directories. This does not apply to the `sql_statements` level (i.e., `sql_statements/4.35.1.sql` is not automatically run, but `sql_statements/cron/your_new_file.sql` will be.)

- `cron/`
    - `schedule_cron_job.sql`
      - Creates a stored procedure which schedules a new pg_cron job to execute a specified stored procedure at intervals defined by a cron schedule string, **if a job with the same command doesn't already exist**.
      - e.g., `CALL schedule_cron_job('0-59 * * * *', 'compile_imagegen_stats_totals');`
- `stored_procedures`
  - `compile_*gen_stats_*.sql`
    - These files defined stored procedures which populated the `compiled_*` tables and generally represent minute/hour/day/total statistics about generations.
  - `cron_jobs/`
    - Schedules any stats compile jobs via `schedule_cron_job`. 