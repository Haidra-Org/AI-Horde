-- SPDX-FileCopyrightText: 2024 Tazlin
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE OR REPLACE PROCEDURE compile_imagegen_stats_totals()
LANGUAGE plpgsql
AS $$
DECLARE
    count_minute INTEGER;
    count_hour INTEGER;
    count_day INTEGER;
    count_month INTEGER;
    count_total BIGINT;
    ps_minute BIGINT;
    ps_hour BIGINT;
    ps_day BIGINT;
    ps_month BIGINT;
    ps_total BIGINT;
BEGIN
    -- Calculate image counts
    SELECT COUNT(*) INTO count_minute FROM image_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 minute';
    SELECT COUNT(*) INTO count_hour FROM image_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 hour';
    SELECT COUNT(*) INTO count_day FROM image_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 day';
    SELECT COUNT(*) INTO count_month FROM image_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '30 days';
    SELECT COUNT(*) INTO count_total FROM image_gen_stats;

    -- Calculate pixel sums
    SELECT COALESCE(SUM(width * height * steps), 0) INTO ps_minute FROM image_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 minute';
    SELECT COALESCE(SUM(width * height * steps), 0) INTO ps_hour FROM image_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 hour';
    SELECT COALESCE(SUM(width * height * steps), 0) INTO ps_day FROM image_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '1 day';
    SELECT COALESCE(SUM(width * height * steps), 0) INTO ps_month FROM image_gen_stats WHERE finished >= (NOW() at time zone 'utc') - INTERVAL '30 days';
    SELECT COALESCE(SUM(width * height * steps), 0) INTO ps_total FROM image_gen_stats;

    -- Insert compiled statistics into compiled_image_gen_stats_totals
    INSERT INTO compiled_image_gen_stats_totals (
        created, minute_images, minute_pixels, hour_images, hour_pixels,
        day_images, day_pixels, month_images, month_pixels, total_images, total_pixels
    ) VALUES (
        (NOW() at time zone 'utc'), count_minute, ps_minute, count_hour, ps_hour,
        count_day, ps_day, count_month, ps_month, count_total, ps_total
    );
END;
$$;
