-- SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE OR REPLACE PROCEDURE compile_imagegen_stats_models()
LANGUAGE plpgsql
AS $$
BEGIN
    WITH model_stats AS (
        SELECT
            kim.id as model_id,
            igs.model as model_name,
            CASE
                WHEN kim.id IS NOT NULL THEN 'known'
                ELSE 'custom'
            END as model_state,
            COUNT(*) FILTER (WHERE igs.finished >= (NOW() at time zone 'utc') - INTERVAL '1 day') as day_images,
            COUNT(*) FILTER (WHERE igs.finished >= (NOW() at time zone 'utc') - INTERVAL '30 days') as month_images,
            COUNT(*) as total_images
        FROM
            image_gen_stats as igs
            LEFT JOIN known_image_models as kim ON igs.model = kim.name
        GROUP BY
            igs.model, kim.id
    )
    INSERT INTO compiled_image_gen_stats_models (created, model_id, model_name, model_state, day_images, month_images, total_images)
    SELECT
            (NOW() at time zone 'utc'),
            model_id,
            model_name,
            model_state,
            day_images,
            month_images,
            total_images
    FROM
        model_stats;
END; $$;
