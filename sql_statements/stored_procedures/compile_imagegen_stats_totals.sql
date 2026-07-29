-- SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE OR REPLACE PROCEDURE compile_imagegen_stats_totals()
LANGUAGE plpgsql
AS $$
DECLARE
    -- image_gen_stats is append-only and never pruned, so an all-time COUNT/SUM
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
    all_images BIGINT;
    all_pixels BIGINT;
    delta_images BIGINT;
    delta_pixels BIGINT;
    count_minute BIGINT;
    count_hour BIGINT;
    count_day BIGINT;
    count_month BIGINT;
    ps_minute BIGINT;
    ps_hour BIGINT;
    ps_day BIGINT;
    ps_month BIGINT;
BEGIN
    -- One clock reading for every window so the snapshot is internally consistent.
    cutoff := (NOW() at time zone 'utc');

    SELECT last_stat_id, total_images, total_pixels
    INTO prev_id, all_images, all_pixels
    FROM compiled_image_gen_stats_totals
    ORDER BY created DESC
    LIMIT 1;

    SELECT COALESCE(MAX(id), 0) - watermark_lag INTO new_id FROM image_gen_stats;
    IF new_id < 0 THEN
        new_id := 0;
    END IF;

    IF prev_id IS NULL THEN
        -- No usable running total to extend (first run, or the previous snapshot
        -- predates the watermark column), so pay for one full-table pass to
        -- establish the baseline.
        SELECT COUNT(*), COALESCE(SUM(width * height * steps), 0)
        INTO all_images, all_pixels
        FROM image_gen_stats WHERE id <= new_id;
    ELSE
        -- A shrinking watermark would double-count, so never move it backwards.
        IF new_id < prev_id THEN
            new_id := prev_id;
        END IF;
        SELECT COUNT(*), COALESCE(SUM(width * height * steps), 0)
        INTO delta_images, delta_pixels
        FROM image_gen_stats WHERE id > prev_id AND id <= new_id;
        all_images := all_images + delta_images;
        all_pixels := all_pixels + delta_pixels;
    END IF;

    -- Every bounded window is a subset of the 30 day window, so one index scan
    -- over `finished` answers all eight aggregates.
    SELECT
        COUNT(*) FILTER (WHERE finished >= cutoff - INTERVAL '1 minute'),
        COALESCE(SUM(width * height * steps) FILTER (WHERE finished >= cutoff - INTERVAL '1 minute'), 0),
        COUNT(*) FILTER (WHERE finished >= cutoff - INTERVAL '1 hour'),
        COALESCE(SUM(width * height * steps) FILTER (WHERE finished >= cutoff - INTERVAL '1 hour'), 0),
        COUNT(*) FILTER (WHERE finished >= cutoff - INTERVAL '1 day'),
        COALESCE(SUM(width * height * steps) FILTER (WHERE finished >= cutoff - INTERVAL '1 day'), 0),
        COUNT(*),
        COALESCE(SUM(width * height * steps), 0)
    INTO count_minute, ps_minute, count_hour, ps_hour, count_day, ps_day, count_month, ps_month
    FROM image_gen_stats
    WHERE finished >= cutoff - INTERVAL '30 days';

    INSERT INTO compiled_image_gen_stats_totals (
        created, minute_images, minute_pixels, hour_images, hour_pixels,
        day_images, day_pixels, month_images, month_pixels, total_images, total_pixels,
        last_stat_id
    ) VALUES (
        cutoff, count_minute, ps_minute, count_hour, ps_hour,
        count_day, ps_day, count_month, ps_month, all_images, all_pixels,
        new_id
    );
END;
$$;
