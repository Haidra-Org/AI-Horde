-- Show text generations per client for last month
SELECT
    COUNT(*), 
    CASE 
        WHEN client_agent LIKE '%Agnaistic%' THEN 'Agnaistic'
        WHEN client_agent LIKE '%ArtBot%' THEN 'ArtBot'
        WHEN client_agent LIKE '%DreamDiffusion%' THEN 'DreamDiffusion'
        WHEN client_agent LIKE '%KoboldAiLite%' THEN 'KoboldAI Lite'
        WHEN client_agent LIKE '%KoboldAI%' THEN 'KoboldAI'
        WHEN client_agent LIKE '%SillyTavern%' THEN 'SillyTavern'
        WHEN client_agent LIKE '%Tavern%' THEN 'TavernAI'
        WHEN client_agent LIKE '%llm-horde%' THEN 'llm-horde'
        WHEN client_agent LIKE '%ZeldaFan-Discord-Bot%' THEN 'ZeldaFan Discord Bot'
        WHEN client_agent LIKE '%@zeldafan0225/stable_horde%' THEN 'ZeldaFan SDK'
        WHEN client_agent LIKE '%KoboldHordeDiscordBot%' THEN 'Kobold Horde Discord Bot'
        WHEN client_agent LIKE '%cli_request_scribe.py%' THEN 'Scribe CLI'
        WHEN client_agent LIKE '%KoboldCppEmbedWorker%' THEN 'KoboldCpp Embed Worker'
        WHEN client_agent LIKE '%Larimar%' THEN 'Larimar'
        WHEN client_agent LIKE '%unknown%' THEN 'Unknown'
        ELSE 'Unknown'
    END AS Client
FROM
    text_gen_stats
WHERE created >= now()::date - interval '1 month'
GROUP BY Client;
