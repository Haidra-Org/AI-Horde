-- SPDX-FileCopyrightText: 2024 2022 Tazlin
-- SPDX-FileCopyrightText: 2024 Tazlin
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE OR REPLACE PROCEDURE compile_textgen_stats_models()
LANGUAGE plpgsql
AS $$
BEGIN
    WITH model_stats AS (
        SELECT 
            tgs.model as model_name,
            COUNT(*) FILTER (WHERE tgs.finished >= (NOW() at time zone 'utc') - INTERVAL '1 day') as day_requests,
            COUNT(*) FILTER (WHERE tgs.finished >= (NOW() at time zone 'utc') - INTERVAL '30 days') as month_requests,
            COUNT(*) as total_requests
        FROM 
            text_gen_stats as tgs
        GROUP BY 
            tgs.model
    )
    INSERT INTO compiled_text_gen_stats_models (created, model, day_requests, month_requests, total_requests)
    SELECT 
        (NOW() at time zone 'utc'),
        model_name,
        day_requests,
        month_requests,
        total_requests
    FROM 
        model_stats;
    COMMIT;
END; $$;
