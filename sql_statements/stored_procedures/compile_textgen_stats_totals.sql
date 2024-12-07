-- SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE OR REPLACE PROCEDURE compile_textgen_stats_totals()
LANGUAGE plpgsql
AS $$
DECLARE
    count_minute INTEGER;
    count_hour INTEGER;
    count_day INTEGER;
    count_month INTEGER;
    count_total INTEGER;
    tokens_minute INTEGER;
    tokens_hour INTEGER;
    tokens_day INTEGER;
    tokens_month INTEGER;
    tokens_total BIGINT;
BEGIN
    -- Calculate request counts
    SELECT COUNT(*) INTO count_minute FROM text_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 minute';
    SELECT COUNT(*) INTO count_hour FROM text_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 hour';
    SELECT COUNT(*) INTO count_day FROM text_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 day';
    SELECT COUNT(*) INTO count_month FROM text_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '30 days';
    SELECT COUNT(*) INTO count_total FROM text_gen_stats;

    -- Calculate token sums
    SELECT COALESCE(SUM(max_length), 0) INTO tokens_minute FROM text_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 minute';
    SELECT COALESCE(SUM(max_length), 0) INTO tokens_hour FROM text_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 hour';
    SELECT COALESCE(SUM(max_length), 0) INTO tokens_day FROM text_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 day';
    SELECT COALESCE(SUM(max_length), 0) INTO tokens_month FROM text_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '30 days';
    SELECT COALESCE(SUM(max_length), 0) INTO tokens_total FROM text_gen_stats;

    -- Insert compiled statistics into compiled_text_gen_stats_totals
    INSERT INTO compiled_text_gen_stats_totals (
        created, minute_requests, minute_tokens, hour_requests, hour_tokens,
        day_requests, day_tokens, month_requests, month_tokens, total_requests, total_tokens
    ) VALUES (
        (NOW() at time zone 'utc'), count_minute, tokens_minute, count_hour, tokens_hour,
        count_day, tokens_day, count_month, tokens_month, count_total, tokens_total
    );
END;
$$;
