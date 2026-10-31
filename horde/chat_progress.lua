-- SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

-- Appends one progress delta under a chat generation's lease, atomically.
-- KEYS[1]: the lease hash of the generation; KEYS[2]: the request's progress stream.
-- ARGV[1]: generation id; ARGV[2]: seq; ARGV[3]: content; ARGV[4]: the most entries one generation may append;
-- ARGV[5]: the stream expiry in seconds; ARGV[6]: the approximate MAXLEN the stream is trimmed to; ARGV[7]: the
-- reasoning content, empty when the delta carries none.
-- Returns {outcome, seq}: the accepted seq for 'accepted' and 'duplicate', the expected seq for 'gap', the refused
-- seq for 'over_cap' and 0 for 'no_lease'. 'accepted' and 'duplicate' carry a third element, 1 when the request was
-- cancelled and 0 otherwise. Sequence state (seq, bytes, last, last_reasoning) lives in the lease hash and expires
-- with it.
local lease_key = KEYS[1]
local stream_key = KEYS[2]
local seq = tonumber(ARGV[2])
local content = ARGV[3]
local reasoning_content = ARGV[7]
if redis.call('EXISTS', lease_key) == 0 then
    return {'no_lease', 0}
end
-- A cancelled lease still takes the delta, since the worker may have generated it before it read the flag; the reply
-- carries the flag so the worker stops.
local cancelled = 0
if redis.call('HGET', lease_key, 'cancelled') == '1' then
    cancelled = 1
end
local last_seq = tonumber(redis.call('HGET', lease_key, 'seq') or '0')
if last_seq > 0 and seq == last_seq and redis.call('HGET', lease_key, 'last') == content
        and (redis.call('HGET', lease_key, 'last_reasoning') or '') == reasoning_content then
    return {'duplicate', last_seq, cancelled}
end
if seq ~= last_seq + 1 then
    return {'gap', last_seq + 1}
end
if seq > tonumber(ARGV[4]) then
    return {'over_cap', seq}
end
local total_bytes = tonumber(redis.call('HGET', lease_key, 'bytes') or '0') + string.len(content)
    + string.len(reasoning_content)
if total_bytes > tonumber(redis.call('HGET', lease_key, 'max_bytes')) then
    return {'over_cap', seq}
end
if redis.call('HGET', lease_key, 'fake') ~= '1' then
    -- Each entry carries the lease's worker and model so a reader can fingerprint the reply from its first delta. An
    -- entry appended under a lease without the model field leaves the model out.
    local entry = {'gen', ARGV[1], 'seq', seq, 'content', content}
    if reasoning_content ~= '' then
        table.insert(entry, 'reasoning_content')
        table.insert(entry, reasoning_content)
    end
    for _, lease_field in ipairs({'worker', 'model'}) do
        local lease_value = redis.call('HGET', lease_key, lease_field)
        if lease_value then
            table.insert(entry, lease_field)
            table.insert(entry, lease_value)
        end
    end
    redis.call('XADD', stream_key, 'MAXLEN', '~', ARGV[6], '*', unpack(entry))
    redis.call('EXPIRE', stream_key, ARGV[5])
end
redis.call('HSET', lease_key, 'seq', seq, 'bytes', total_bytes, 'last', content, 'last_reasoning', reasoning_content)
return {'accepted', seq, cancelled}
