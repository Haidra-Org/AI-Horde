CREATE EXTENSION IF NOT EXISTS pg_cron;

CREATE OR REPLACE PROCEDURE schedule_cron_job(
  p_schedule TEXT,
  p_stored_procedure TEXT
)
LANGUAGE plpgsql
AS $$
DECLARE
  job_exists boolean;
BEGIN
  SET search_path TO cron, public;
  -- Check if the job already exists
  SELECT EXISTS (
    SELECT 1 FROM cron.job 
    WHERE command = format($CRON$ CALL %s(); $CRON$, p_stored_procedure)
  ) INTO job_exists;
  
  -- If the job doesn't exist, schedule it
  IF NOT job_exists THEN
    PERFORM cron.schedule(p_schedule, format($CRON$ CALL %s(); $CRON$, p_stored_procedure));
    RAISE NOTICE 'Cron job scheduled successfully for stored procedure: %', p_stored_procedure;
  ELSE
    RAISE NOTICE 'Cron job already exists for stored procedure: %. Skipping scheduling.', p_stored_procedure;
  END IF;
END $$;
