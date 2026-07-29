-- SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE OR REPLACE PROCEDURE compile_textgen_stats_totals()
LANGUAGE plpgsql
AS $$
DECLARE
    -- text_gen_stats is append-only and never pruned, so an all-time COUNT/SUM
    -- reads the whole table. The all-time figures are instead carried forward
    -- from the previous snapshot and extended with only the rows appended since
    -- that snapshot's `last_stat_id` watermark.
    --
    -- The watermark stops this many ids short of the sequence head because a row
    -- whose id is already allocated may not have committed yet. Rows inside that
    -- gap are folded in on a later run, so the all-time figures trail the true
    -- values by at most this many rows.
    watermark_lag CONSTANT BIGINT := 1000;
    cutoff TIMESTAMP;
    prev_id BIGINT;
    new_id BIGINT;
    all_requests BIGINT;
    all_tokens BIGINT;
    delta_requests BIGINT;
    delta_tokens BIGINT;
    -- The window token sums exceed the range of INTEGER, so every accumulator
    -- here is BIGINT.
    count_minute BIGINT;
    count_hour BIGINT;
    count_day BIGINT;
    count_month BIGINT;
    tokens_minute BIGINT;
    tokens_hour BIGINT;
    tokens_day BIGINT;
    tokens_month BIGINT;
BEGIN
    -- One clock reading for every window so the snapshot is internally consistent.
    cutoff := (NOW() at time zone 'utc');

    SELECT last_stat_id, total_requests, total_tokens
    INTO prev_id, all_requests, all_tokens
    FROM compiled_text_gen_stats_totals
    ORDER BY created DESC
    LIMIT 1;

    SELECT COALESCE(MAX(id), 0) - watermark_lag INTO new_id FROM text_gen_stats;
    IF new_id < 0 THEN
        new_id := 0;
    END IF;

    IF prev_id IS NULL THEN
        -- No usable running total to extend (first run, or the previous snapshot
        -- predates the watermark column), so pay for one full-table pass to
        -- establish the baseline.
        SELECT COUNT(*), COALESCE(SUM(max_length), 0)
        INTO all_requests, all_tokens
        FROM text_gen_stats WHERE id <= new_id;
    ELSE
        -- A shrinking watermark would double-count, so never move it backwards.
        IF new_id < prev_id THEN
            new_id := prev_id;
        END IF;
        SELECT COUNT(*), COALESCE(SUM(max_length), 0)
        INTO delta_requests, delta_tokens
        FROM text_gen_stats WHERE id > prev_id AND id <= new_id;
        all_requests := all_requests + delta_requests;
        all_tokens := all_tokens + delta_tokens;
    END IF;

    -- Every bounded window is a subset of the 30 day window, so one index scan
    -- over `finished` answers all eight aggregates.
    SELECT
        COUNT(*) FILTER (WHERE finished >= cutoff - INTERVAL '1 minute'),
        COALESCE(SUM(max_length) FILTER (WHERE finished >= cutoff - INTERVAL '1 minute'), 0),
        COUNT(*) FILTER (WHERE finished >= cutoff - INTERVAL '1 hour'),
        COALESCE(SUM(max_length) FILTER (WHERE finished >= cutoff - INTERVAL '1 hour'), 0),
        COUNT(*) FILTER (WHERE finished >= cutoff - INTERVAL '1 day'),
        COALESCE(SUM(max_length) FILTER (WHERE finished >= cutoff - INTERVAL '1 day'), 0),
        COUNT(*),
        COALESCE(SUM(max_length), 0)
    INTO count_minute, tokens_minute, count_hour, tokens_hour, count_day, tokens_day, count_month, tokens_month
    FROM text_gen_stats
    WHERE finished >= cutoff - INTERVAL '30 days';

    INSERT INTO compiled_text_gen_stats_totals (
        created, minute_requests, minute_tokens, hour_requests, hour_tokens,
        day_requests, day_tokens, month_requests, month_tokens, total_requests, total_tokens,
        last_stat_id
    ) VALUES (
        cutoff, count_minute, tokens_minute, count_hour, tokens_hour,
        count_day, tokens_day, count_month, tokens_month, all_requests, all_tokens,
        new_id
    );
END;
$$;
