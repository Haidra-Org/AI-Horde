-- SPDX-FileCopyrightText: 2024 Tazlin
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

CALL schedule_cron_job('0 1 1-31 * *', 'compile_imagegen_stats_models');
CALL schedule_cron_job('0-59 * * * *', 'compile_imagegen_stats_totals');
CALL schedule_cron_job('0 1 1-31 * *', 'compile_textgen_stats_models');
CALL schedule_cron_job('0-59 * * * *', 'compile_textgen_stats_totals');
