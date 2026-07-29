-- SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE OR REPLACE PROCEDURE compile_imagegen_stats_models()
LANGUAGE plpgsql
AS $$
DECLARE
    -- image_gen_stats is append-only and never pruned, so an all-time per-model
    -- COUNT reads the whole table. The per-model all-time counts are instead
    -- carried forward from the previous snapshot and extended with only the rows
    -- appended since that snapshot's `last_stat_id` watermark. The day and month
    -- figures cover sliding windows and are recomputed from `finished` on every
    -- run; that window is a small fraction of the table and is served by
    -- ix_image_gen_stats_finished.
    --
    -- The watermark stops this many ids short of the sequence head because a row
    -- whose id is already allocated may not have committed yet. Rows inside that
    -- gap are folded in on a later run, so the all-time counts trail the true
    -- values by at most this many rows.
    watermark_lag CONSTANT BIGINT := 1000;
    cutoff TIMESTAMP;
    baseline_created TIMESTAMP;
    prev_id BIGINT;
    new_id BIGINT;
BEGIN
    -- One clock reading for every window so the snapshot is internally consistent.
    cutoff := (NOW() at time zone 'utc');

    -- The most recent snapshot that can be extended. A snapshot written before
    -- the watermark column existed carries no watermark and is skipped; folding
    -- every id above an older watermarked snapshot is correct regardless of what
    -- was written in between.
    SELECT created, last_stat_id
    INTO baseline_created, prev_id
    FROM compiled_image_gen_stats_models
    WHERE last_stat_id IS NOT NULL
    ORDER BY created DESC
    LIMIT 1;

    -- With no snapshot to extend, `baseline_created` stays NULL so the baseline
    -- below selects no rows and the id range starts at 0, which makes the first
    -- run one full-table pass that establishes the baseline for every run after.
    prev_id := COALESCE(prev_id, 0);

    SELECT COALESCE(MAX(id), 0) - watermark_lag INTO new_id FROM image_gen_stats;
    -- A shrinking watermark would double-count, so never move it backwards.
    IF new_id < prev_id THEN
        new_id := prev_id;
    END IF;

    WITH windowed AS (
        SELECT
            igs.model AS model_name,
            COUNT(*) FILTER (WHERE igs.finished >= cutoff - INTERVAL '1 day') AS day_images,
            COUNT(*) AS month_images
        FROM image_gen_stats AS igs
        WHERE igs.finished >= cutoff - INTERVAL '30 days'
        GROUP BY igs.model
    ),
    contributions AS (
        SELECT model_name, total_images
        FROM compiled_image_gen_stats_models
        WHERE created = baseline_created
        UNION ALL
        SELECT igs.model, COUNT(*)
        FROM image_gen_stats AS igs
        WHERE igs.id > prev_id AND igs.id <= new_id
        GROUP BY igs.model
        UNION ALL
        -- A model whose entire history sits inside the watermark gap has no
        -- all-time contribution yet and would otherwise be missing from the
        -- snapshot until the gap is folded in on a later run.
        SELECT model_name, 0 FROM windowed
    ),
    model_stats AS (
        SELECT model_name, SUM(total_images) AS total_images
        FROM contributions
        GROUP BY model_name
    )
    INSERT INTO compiled_image_gen_stats_models (
        created, model_id, model_name, model_state, day_images, month_images, total_images, last_stat_id
    )
    SELECT
        cutoff,
        kim.id,
        ms.model_name,
        CASE
            WHEN kim.id IS NOT NULL THEN 'known'
            ELSE 'custom'
        END,
        COALESCE(w.day_images, 0),
        COALESCE(w.month_images, 0),
        ms.total_images,
        new_id
    FROM
        model_stats AS ms
        LEFT JOIN windowed AS w ON w.model_name = ms.model_name
        LEFT JOIN known_image_models AS kim ON kim.name = ms.model_name;
END; $$;
