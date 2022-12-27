# Changelog

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
