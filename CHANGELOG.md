# Changelog

# 4.15.5

Fixed worker blacklist

# 4.15.4

Avoid null inject_trigger in loras

# 4.15.3

Support lora's `inject_trigger`

# 4.15.2

* Prevents more than 5 loras per image
* display VPN role
* Allows scribes to send empty string as results
* Using VPN shouldn't keep increasing a user's suspicion endlessly.

# 4.15.1

* Removed "soft_prompt" from the payload to Scribes as it's sent elsewhere.

# 4.15.0

* Added support for LoRas in payloads for SD
* Improved retrieval filtering

# 4.14.0

* Enabled NN-model based kudos-calculation
* Re-enabled automatically putting workers as paused when suspicion gets too high
* Added `dry_run` payload to `/async` endpoints. When specified, instead of generating, it will return the expected kudos cost.

# 4.13.0

* Can now specify that the worker array in your generation request should act as a worker blacklist, instead of a whitelist.

   When doing so, the generation consumes 10% more kudos as it causes suboptimal use of Horde resources.

* Prevented shared key kudos going negative
* Allow PATCH calls from UIs

# 4.12.1

* Limited kudos transfers to 1/sec to prevent race condition abuse
* Added KudosTransferLog ORM class to help me catch abusers.

# 4.12.0

**Added Shared Keys**. Now each user can generate a number of shared keys which they can give to others to use.
When using a shared key to generate, the request pretend act as if it was that user. 

However shared keys cannot be used for any other purpose than generating, so they cannot be abused. They are thus meant to be a lower-security option for sharing one's priority, without having to transfer kudos all the time.

Shared keys can be created with an optional limited amount of kudos to use, and/or an expiry date. 
Regardless of what the shared key kudos limit is, the request will use the full kudos priority of its owner.

New Endpoints:

* /v2/sharedkeys PUT
* /v2/sharedkeys GET/PATCH/DELETE

Check api documentation for payloads required.

* Shortened limiter for kudos transfers to avoid abuse

# 4.11.2

* Increased stipend for moderators
* Fixed bug when retrieving an uncached user with monthly kudos

# 4.11.0

* Added new `vpn` role which allows someone to run a worker behind a VPN without being trusted.

# 4.10.0

* Weights now do not cost kudos (preparation for comfy switch)
* Allows text models to be named by appending the horde `::user#id` at the end. 
  A worker can only offer such a model when the worker's owner matches the name in the model name.
  This will allow test models to be served in a way that someone cannot poison the data.
* Cached user GET.


# 4.9.0

* Refactored user objects so that we can specify an open-ended amount of roles, without having to add a new column for each
* Added new `customizer` role which allows someone to serve unknown Stable Diffusion models. They will be considered to be using SD 1.5 baseline
* Increased threshold for trusting users. Becoming trusted also requires at least a week of wait-time.
* Re-activated limit on workers per IP. Trusted users can bypass it.
* Untrusted users now can have only 3 workers. Trusted workers by default can go up to 20.
* Models endpoing now shows queued jobs per model
* Disabled VPN for workers unless they're trusted or patreons

# 4.8.0

* `is_possible` is back! Now each request's status will report whether the current payload can be completed
* cli_requests has moved to a new repository: https://github.com/db0/AI-Horde-CLI


# 4.7.0

* The user list is back! It now only returns 25 users at a time, but you can retrieve any page
  It accepts sorting by kudos or by account age. Kudos is sorted descending. Account age sorted ascending.
* Now model list should display accurate performance average per model

# 4.6.8

* Reduced kudos calculation for unapproved textgen models a bit.

# 4.6.7

* slow_worker for Dreamers now are < 0.5MPS/s
* Added slow_worker for Alchemists

# 4.6.6

`/api/v2/users/user_id` won't block non-mod API keys, but will just use the lowest permissions

# 4.6.5

* Prevent a1111 webui worker from picking up hires-fix jobs as it seems to be failing
* Make max_pixels column bigint

# 4.6.4

Whitelisted workers for Waiting prompts are limited to 5

# 4.6.3

Re-enabled inpainting

# 4.6.2

Fixed incorrectly using `.seconds` instead of `.total_seconds()` in timedeltas

# 4.6.1

* Added `max_tiles` to alchemist pop. Now alchemists will not pick up source images with higher amount of 512x512 tiles than their max_tiles
* alchemist post-processing reward now based on max_tiles

# 4.6.0

* Added `NMKD_Siax` and `4x_AnimeSharp` Upscalers (@ResidentChief)

# 4.5.1

* Improved total_counts() calculation and speed

# 4.5.0

* Renamed Horde Interrogation Worker to Horde Alchemist
* Horde Alchemist can now also perform post-processing on images, instead of only interrogation. Form names are the various post-processor names.
* post-processed images will be likewise uploaded to R2, and the form result will be the download URL

# 4.4.1

* Added bridge version control for `strip_background` and `return_control_map`

# 4.4.0

* Fixed worker performances showing wrong
* Adjusted so that max resolution and tokens without kudos upfront is based on amount of workers active
* Added new `api/v2/generate/async` post-processor `strip_background`. Will remove background from image (@ResidentChief)
* Added new `api/v2/generate/async` param `return_control_map`. Will return control map instead of final image (@ResidentChief)

# 4.3.1

* Added new option for `/async` (both text and image): `slow_workers`.
   * If True (Default), the request will function as currently
   * If False, the request will only be picked up by workers who have a decent speed (0.3 MPS/s for Image, 2 tokens/s for Text). However selecting this option will incur a 20% Kudos consumption penalty and require upfront kudos.  
   
   The purpose of this option is to give people the ability to onboard slower workers while also allowing other people to avoid those workers if needed.
* Added check for load on Text Gen and requirement for upfront kudos when requesting too many tokens while load is high   

# 4.3.0

* Added RealESRGAN_x4plus_anime_6B post-processor (@ResidentChief)
* Added DDIM sampler (@ResidentChief)

# 4.2.0

* Added regex transparent replacements instead of IP blocks. 
* New arg for `/api/v2/generate/async`: `replacement_filter`. When True (Default), it will transparently replace underage context in CSAM-detected prompts. When false (or when prompt too large) will IP block instead.
* NSFW models which hit their lightweight CSAM filter, will always replace instead of giving an error. This is to avoid people reverse engineering the CSAM regex through trial and error

# 4.1.9

* Tweaks on patreon supporters
* Workers can report CSAM prompts
* CSAM prompts are recorded
* fix: whitespace convertor

# 4.1.8

Age check shouldn't apply to text

# 4.1.7

* Attempt to fix duplicating seeds

# 4.1.6

* Fixed default maintenance msg
* Disabled inpainting temporarily

# 4.1.5

* Added CSAM trigger detection

# 4.1.4

* Adjust patreon rewards
* Flaffed users have less priority
* stopped removing images from R2 from threads.
* Prevents 'colab' and 'tpu' in worker names.

# 4.1.1

* Can send images as control

# 4.1.0

* Added recaptcha to login

# 4.0.11

* Fixed horde modes

# 4.0.10

* Preserve image alpha channel

# 4.0.9

* Added backup local redis cache

# 4.0.8

* Fixed limiter for text status
* fixed error when invalid priority usernames sent to interrogation

# 4.0.6

* Improved filtering for WP priority increase to avoid deadlocks

# 4.0.5

* Increased kudos consumption by 1 per weight.
* Increased ttl according to CN and weights
* Weights over 12 require upfront kudos due to extra load on workers

# 4.0.4

* Fixed Diffusers models and SD2@512 being silently ignored.

# 4.0.3

* Fixed IP ban being in minutes instead of seconds
* Support for ipv6 on workers

# 4.0.2

* Avoids removing trusted status when kai kudos is migrated
* Fix bad frontpage token divisor
* Fix worker lookup ignoring text workers

# 4.0.0

* Massive Refactoring merged KoboldAI Horde into Stable Horde! Now you can request text generations from the same place you request your image generations and your kudos are in the same place!
   All new endpoints are under `api/v2/generate/text/` and they work identically to image endpoints but with slightly different payloads. No need to use `/check` as well
* Unfortunately Users and Worker statistics and kudos could not be transferred, but KoboldAI users can transfer their existing KAI Horde kudos using a dedicated interface
* `/api/v2/user`: `contributions` and `usage` fields are now obsolete. Switch to the `records` field as they will be decommissioned
* `/stats/text/totals/`: New endpoints for text statistics
* `/api/v2/workers` should now show image, interrogation and text workers. You can filter the list using the `type` query. For example `/api/v2/workers?type=text`
* `/v2/status/models` should now show image and text workers. You can filter the list using the `type` query. For example `/v2/status/models?type=text`
   By default it is using type=image, to avoid breaking existing UIs, **but this will be removed eventually so ensure your UIs take that into account**.
* `/v2/status/performance` Now shows text performance as well

## 3.11.2

* Fix for source images being deleted from r2 before being used
* Added check for controlnet models (@ResidentChief)
* Fixed Database extra bandwidth load
* Added version to heartbeat

## 3.11.1

* All generations now uploaded to R2 and simply converted to b64 when requested by the client
* All source images now uploaded to R2. Older worker versions will still receive b64 sources, by converting them at the point of pull. So update your workers
* Upped the prompt limit to 7500

## 3.10.1

* Added stats endpoings `/api/v2/stats/img/totals` and `/api/v2/stats/img/models`
* Added safety for int overflow on kudos details

## 3.10.0

* Started recording image stats
* Worker info now shows if they're capable of post-processing
* Removed prompt length limit
* Added support for hires_fix

## 3.9.0

* Response headers now report node information

## 3.8.0

* Improved Filter Regex

## 3.7.0

* Added the ability to mark users as flagged. A flagged user cannot transfer or be transferred kudos and all their workers are set into permanent maintenance

## 3.6.0

* Workers can now report back if a generation is faulted or censored by using the new "state" key. The "censored" key is now obolete and will be removed.
* Horde will now ignore unknown models
* Can now send "1girl", but not "girl" to Hentai Diffusion

## 3.5.0

* Worker Bridges now have to send a new field in the pop payload called bridge_agent. 
    It should be in the form of `name:version:url`. For example `AI Horde Worker:11:https://github.com/db0/AI-Horde-Worker`

   This will allow people to better know the capabilities of each worker 

* Exposed bridge_agent in the worker info

* api/v2/generate/async now supports `tiling` boolean in `params`. Check API/doc

## 3.4

* Restricted generic terms like `girl` and `boy` from NSFW models
* added `shared` boolean on `api/v2/generate/status` to know if a generation was shared to create the LAION dataset.

## 3.3

* R2 uploads now defaults to True
* Fixed uploading images as censored=True causing a 404
* Fixed not being able to serve models with >30 chars
* Added regex endpoint for filters
* When trying to submit on an aborted gen, will get a different error than a duplicate one

## 3.2 

* Added Filtering API

## v2.7

### Features 

* Increased the jobs dropped needed to think a worker is suspicious
* Added thread locking for starting generations to avoid the n going negative

# Changelog

## v2.6

### Features 

* Added post-processing capabilities. GFPGAN and RealESRGAN_x4plus
* Added clean shutdown capability
* Added rate limiter information to headers (@jamdon2)

### Tweaks

* Adaptive samplers now consume kudos as if they are at 50 steps always.

### API

Please check the API documentation for each new field.

### Model `ModelGenerationInputStable`

Used in `/v2/generate/async`, `/v2/generate/sync`

* Added 'post_processing' key
* increased amount of models allows in 'models' key

### endpoint `/v2/status/modes`

* Added 'shutdown' key



## v2.5

### Features

The horde workers can now run multiple threads. The horde has been adjusted to show proper speed when workers have multiple threads.

### Countermeasures

* Anon user has a different concurrency per model. Anon can only queue 10 images per worker serving that model. That means anon cannot request images for models that don't have any workers! I had to add this countermeasure because I noticed someone queueing 500 images on a model that had very few workers, therefore choking the whole queue for other anon usage.
* Lowered limits before upfront kudos are required. 
   * Now the resolution threshold is based on how many concurrent requests are currently in the queue to a min of 576x576 which will remain always available without kudos.
   * steps threshold lowered to 50
   * pseudonymous users start with 14 kudos, which will allow them to always have a bit more kudos to do some extra steps or resolution.
   * oauth users start with 25 kudos, which will allow them to always even more kudos to do some extra steps or resolution compared to pseudonymous.
* request status check is now cached for 1 second to reduce server load.
* pseudonymous and oauth users cannot transfer their starting kudos baseline.


### API

Please check the API documentation for each new field.

* The new online key for workers will allow to mark which workers are currently online for each team
* Team performance now only takes into account online workers

### Model `WorkerDetailsLite`

Used in `/v2/worker/{worker_id}`, `/v2/teams`, `/v2/teams/{team_id}` etc

* Added 'online' key

### Model `WorkerDetailsStable`

Used in `/v2/worker/` and `/v2/worker/{worker_id}`

* Added 'threads' key


## v2.4

* Tweaked Blacklist
* Added 4 new samplers. Cannot use new samplers on img2img yet
* added Karras
* Extra check to avoid two workers picking up the same job
* Source mask can now be sent to img2img
* Added IP timeout for Corrupted Prompts

## v2.3


### Features

* Added ability to create teams and join them with workers. 
    Each worker will record how much kudos, requests and MPS they're generated to their team. This has no utility by itself other than allowinf self-organization.
    The new APIs developed allow **trusted users** to create new teams, but anyone can dedicate their worker to a specific team by using the worker PUT.
    You can list all teams or query a specific one.
* Added cache to the various endpoints. My server started lagging because python started getting overwhelmed from all the calculations. This helps release some of this stress
    at the cost of some data not being up to date. The big APIs like /users and /workers are caching for 10 seconds, while individual users and workers and the model list do so only for a couple seconds. This is mostly to help with the hundreds of users on UIs hammering for details.


### API

Please check the API documentation for each new field.

### New Endpoints

* `/v2/teams/
* `/v2/teams/{team_id}`

### `/v2/worker/{worker_id}`

* Now validation is happening via Fields

#### PUT
* New key: `reset_suspicion`
* New key: `team`
#### GET
* New key: `contact`
* New key: `team`

### `/v2/users/{user_id}`

* Now validation is happening via Fields

#### PUT
* New key: `reset_suspicion`
* New key: `contact`
#### GET
* New key: `contact`



## v2.2

### Features

* New requests for specific server IDs will abort immediately if that ID does not corresponds to a server
* Workers in maintenance can now accept requests from their owner only. Workers in maintenance do not gain kudos for uptime.
* Takes into account denoising strength for kudos calculations on img2img

### Countermeasures

* Tightened filter

### API

Please check the API documentation for each new field.

#### `/v2/generate/check/{id}`

* New key: `kudos`
* New key: `is_possible`

## v2.1

### Features

* Uncompleted jobs will now be restarted. A request which ends up with 3 restarted jobs will abort
* Cancelled requests will reward kudos to workers who've already started the request
* better Kudos calculation
* k_heun and k_dps now count as double steps for kudos
* Allow resolutions higher than 1024x1024 and steps more than 100. But Those requests require the user to have the kudos upfront.

### Countermeasures

* Increase suspicion if workers lose too many jobs in an hour
* suspicious users cannot transfer kudos
* suspicious users cannot receive kudos
* bleached model name
* workers cannot serve more than 30 models
* clients with unsafe IPs will now only connect to trusted workers.

### Bugs

* Prevents negative kudos transfer


## v2.0

This is as far back as my current changelog goes. This header does not contain all the changes until 2.0

### Countermeasures 

* Increased suspicion threshold
* More Corrupt prompts filtering

### Bugs

* Fix bad kudos transfer marshalling
* Avoid runtime error during /users retrieve

### API

Please check the API documentation for each new field.

#### `/v2/generate/async`

Adds inpainting/outpainting support

* New key: `source_processing`
* New key: `source_mask`

#### `/v2/status/models`

Added performance info on models

* New key: `performance`
* New key: `queued`
* New key: `eta`
