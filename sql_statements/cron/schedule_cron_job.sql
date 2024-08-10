-- SPDX-FileCopyrightText: 2024 Tazlin
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE EXTENSION IF NOT EXISTS pg_cron;

CREATE OR REPLACE PROCEDURE schedule_cron_job(
  p_schedule TEXT,
  p_stored_procedure TEXT
)
LANGUAGE plpgsql
AS $$
DECLARE
  existing_schedule TEXT;
  existing_jobid INT;
BEGIN
  SET search_path TO cron, public;

  -- Get the existing schedule and jobid for the stored procedure
  SELECT schedule, jobid
  INTO existing_schedule, existing_jobid
  FROM cron.job
  WHERE command = format($CRON$ CALL %s(); $CRON$, p_stored_procedure);

  -- If the job exists and the schedules don't match, update it
  IF FOUND AND existing_schedule <> p_schedule THEN
    PERFORM cron.unschedule(existing_jobid);
    PERFORM cron.schedule(p_schedule, format($CRON$ CALL %s(); $CRON$, p_stored_procedure));
    RAISE NOTICE 'Cron job schedule updated successfully for stored procedure: %', p_stored_procedure;
  -- If the job doesn't exist, schedule it
  ELSIF NOT FOUND THEN
    PERFORM cron.schedule(p_schedule, format($CRON$ CALL %s(); $CRON$, p_stored_procedure));
    RAISE NOTICE 'Cron job scheduled successfully for stored procedure: %', p_stored_procedure;
  ELSE
    RAISE NOTICE 'Cron job already exists with the same schedule for stored procedure: %. Skipping scheduling.', p_stored_procedure;
  END IF;
END $$;

UPDATE cron.job SET nodename = '';
