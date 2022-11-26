from horde.classes.base import *
from horde.classes.stable.worker import WorkerExtended

class WaitingPrompt(WaitingPrompt):

    @logger.catch(reraise=True)
    def extract_params(self, params, **kwargs):
        self.n = params.pop('n', 1)
        self.jobs = self.n 
        self.steps = params.pop('steps', 50)
        # We assume more than 20 is not needed. But I'll re-evalute if anyone asks.
        if self.n > 20:
            logger.warning(f"User {self.user.get_unique_alias()} requested {self.n} gens per action. Reducing to 20...")
            self.n = 20
        self.width = params.get("width", 512)
        self.height = params.get("height", 512)
        self.models = kwargs.get("models", ['stable_diffusion'])
        self.sampler = params.get('sampler_name', 'k_euler')
        # Silent change
        if self.models == ["stable_diffusion_2.0"]:
            self.sampler = "dpmsolver"
        # The total amount of to pixelsteps requested.
        self.source_image = kwargs.get("source_image", None)
        self.source_processing = kwargs.get("source_processing", 'img2img')
        self.source_mask = kwargs.get("source_mask", None)
        self.censor_nsfw = kwargs.get("censor_nsfw", True)
        self.seed = None
        if 'seed' in params and params['seed'] != None:
            # logger.warning([self,'seed' in params, params])
            self.seed = params.pop('seed')
        self.seed_variation = None
        self.generations_done = 0
        if "seed_variation" in params:
            self.seed_variation = params.pop("seed_variation")
        self.prepare_job_payload(params)
        # To avoid unnecessary calculations, we do it once here.
        self.things = self.width * self.height * self.get_accurate_steps()
        self.total_usage = round(self.things * self.n / thing_divisor,2)
        self.calculate_kudos()

    @logger.catch(reraise=True)
    def prepare_job_payload(self, initial_dict = {}):
        # This is what we send to KoboldAI to the /generate/ API
        self.gen_payload = initial_dict
        self.gen_payload["prompt"] = self.prompt
        # We always send only 1 iteration to Stable Diffusion
        self.gen_payload["batch_size"] = 1
        self.gen_payload["ddim_steps"] = self.steps
        self.gen_payload["seed"] = self.seed

    @logger.catch(reraise=True)
    def get_job_payload(self,procgen):
        if self.seed_variation and self.generations_done > 0:
            self.gen_payload["seed"] += self.seed_variation
            while self.gen_payload["seed"] >= 2**32:
                self.gen_payload["seed"] = self.gen_payload["seed"] >> 32
        else:
            # logger.error(self.seed)
            self.gen_payload["seed"] = self.seed_to_int(self.seed)
            self.generations_done += 1
        if procgen.worker.bridge_version >= 2:
            if not self.nsfw and self.censor_nsfw:
                self.gen_payload["use_nsfw_censor"] = True
        else:
            # These parameters are not used in bridge v1
            for v2_param in ["use_gfpgan","use_real_esrgan","use_ldsr","use_upscaling"]:
                if v2_param in self.gen_payload:
                    del self.gen_payload[v2_param]
            if not self.nsfw and self.censor_nsfw:
                if "toggles" not in self.gen_payload:
                    self.gen_payload["toggles"] = [1, 4, 8]
                elif 8 not in self.gen_payload["toggles"]:
                    self.gen_payload["toggles"].append(8)
            if "denoising_strength" in self.gen_payload:
                del self.gen_payload["denoising_strength"]
        return(self.gen_payload)

    def get_pop_payload(self, procgen):
        # This prevents from sending a payload with an ID when there has been an exception inside get_job_payload()
        payload = self.get_job_payload(procgen)
        if payload:
            prompt_payload = {
                "payload": payload,
                "id": procgen.id,
                "model": procgen.model,
            }
            if self.source_image and procgen.worker.bridge_version > 2:
                prompt_payload["source_image"] = self.source_image
            if procgen.worker.bridge_version > 3:
                prompt_payload["source_processing"] = self.source_processing
                if self.source_mask:
                    prompt_payload["source_mask"] = self.source_mask
        else:
            prompt_payload = {}
            self.faulted = True
        # logger.debug([payload,prompt_payload])
        return(prompt_payload)

    def activate(self):
        # We separate the activation from __init__ as often we want to check if there's a valid worker for it
        # Before we add it to the queue
        super().activate()
        prompt_type = "txt2img"
        if self.source_image:
            prompt_type = self.source_processing
        logger.info(f"New {prompt_type} prompt with ID {self.id} by {self.user.get_unique_alias()} ({self.ipaddr}): w:{self.width} * h:{self.height} * s:{self.steps} * n:{self.n} == {self.total_usage} Total MPs")

    def new_procgen(self, worker):
        return(ProcessingGeneration(self, self._processing_generations, worker))

    def seed_to_int(self, s = None):
        if type(s) is int:
            return s
        if s is None or s == '':
            return random.randint(0, 2**32 - 1)
        n = abs(int(s) if s.isdigit() else int.from_bytes(s.encode(), 'little'))
        while n >= 2**32:
            n = n >> 32
        # logger.debug([s,n])
        return n

    def record_usage(self, raw_things, kudos):
        '''I have to extend this function for the stable cost, to add an extra cost when it's an img2img
        img2img burns more kudos than it generates, due to the extra bandwidth costs to the horde.
        Also extra cost when upscaling
        '''
        if self.source_image:
            kudos = kudos * 1.5
        if 'RealESRGAN_x4plus' in self.gen_payload.get('post_processing', []):
            kudos = kudos * 1.3
        super().record_usage(raw_things, kudos)

    # We can calculate the kudos in advance as they model doesn't affect them
    def calculate_kudos(self):
        result = pow((self.width * self.height) - (64*64), 1.75) / pow((1024*1024) - (64*64), 1.75)
        # We need to calculate the steps, without affecting the actual steps requested
        # because some samplers are effectively doubling their steps
        steps = self.get_accurate_steps()
        self.kudos = round((0.1232 * steps) + result * (0.1232 * steps * 8.75),2)
        # For each post processor in requested, we increase the cost by 20%
        for post_processor in self.gen_payload.get('post_processing', []):
            self.kudos = round(self.kudos * 1.2,2)


    def requires_upfront_kudos(self):
        '''Returns True if this wp requires that the user already has the required kudos to fulfil it
        else returns False
        '''
        queue = self._waiting_prompts.count_totals()["queued_requests"]
        max_res = 1124 - round(queue * 0.9)
        if max_res < 576:
            max_res = 576
            # SD 2.0 requires at least 768 to do its thing
            if max_res < 768 and len(self.models) > 1 and "stable_diffusion_2.0" in self.models:
                max_res = 768
        if max_res > 1024:
            max_res = 1024
        if self.get_accurate_steps() > 50:
            return(True,max_res)
        if self.width * self.height > max_res*max_res:
            return(True,max_res)
        # haven't decided yet if this is a good idea.
        # if 'RealESRGAN_x4plus' in self.gen_payload.get('post_processing', []):
        #     return(True,max_res)
        return(False,max_res)

    def get_accurate_steps(self):
        if self.sampler in ['k_dpm_adaptive']:
            # This sampler chooses the steps amount automatically 
            # and disregards the steps value from the user
            # so we just calculate it as an average 50 steps
            return(50)
        steps = self.steps
        if self.sampler in ['k_heun', "k_dpm_2", "k_dpm_2_a", "k_dpmpp_2s_a"]:
            # These samplerS do double steps per iteration, so they're at half the speed
            # So we adjust the things to take that into account
            steps *= 2
        if self.source_image and self.source_processing == "img2img":
            # 0.8 is the default on nataili
            steps *= self.gen_payload.get("denoising_strength",0.8)
        return(steps)

    def set_job_ttl(self):
        # default is 2 minutes. Then we scale up based on resolution.
        # This will be more accurate with a newer formula
        self.job_ttl = 120
        if self.width * self.height > 2048*2048:
            self.job_ttl = 800
        elif self.width * self.height > 1024*1024:
            self.job_ttl = 400
        elif self.width * self.height > 728*728:
            self.job_ttl = 260
        elif self.width * self.height >= 512*512:
            self.job_ttl = 150

    def log_faulted_job(self):
        source_processing = 'txt2img'
        if self.source_image:
            source_processing = self.source_processing
        logger.warning(f"Faulting waiting {source_processing} prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs")


class ProcessingGeneration(ProcessingGeneration):

    def get_details(self):
        '''Returns a dictionary with details about this processing generation'''
        ret_dict = {
            "img": self.generation,
            "seed": self.seed,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
        }
        return(ret_dict)

    def get_gen_kudos(self):
        # We have pre-calculated them as they don't change per worker
        return(self.owner.kudos)

    def log_aborted_generation(self):
        logger.info(f"Aborted Stale Generation {self.id} ({self.owner.width}x{self.owner.height}x{self.owner.steps}@{self.owner.sampler}) from by worker: {self.worker.name} ({self.worker.id})")


class News(News):

    STABLE_HORDE_NEWS = [
        {
            "date_published": "2022-11-24",
            "newspiece": "Due to the massive increase in demand from the Horde, we have to limit the amount of concurrent anonymous requests we can serve. We will revert this once our infrastructure can scale better.",
            "importance": "Crisis"
        },
        {
            "date_published": "2022-11-24",
            "newspiece": "Stable Diffusion 2.0 has been released and now it is available on the Horde as well.",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-22",
            "newspiece": "A new Stable Horde Bot has been deployed, this time for Mastodon. You can find [the stablehorde_generator}(https://sigmoid.social/@stablehorde_generator) as well as our [official Stable Horde account](https://sigmoid.social/@stablehorde) on sigmoid.social",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-22",
            "newspiece": "We now have [support for the Unreal Engine](https://github.com/Mystfit/Unreal-StableDiffusionTools/releases/tag/v0.5.0) via a community-provided plugin",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-18",
            "newspiece": "The stable horde [now supports post-processing](https://www.patreon.com/posts/post-processing-74815675) on images automatically",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-05",
            "newspiece": "Due to suddenly increased demand, we have adjusted how much requests accounts can request before needing to have the kudos upfront. More than 50 steps will require kudos and the max resolution will be adjusted based on the current horde demand.",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-05",
            "newspiece": "Workers can now [join teams](https://www.patreon.com/posts/teams-74247978) to get aggregated stats.",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-02",
            "newspiece": "The horde can now generate images up to 3072x3072 and 500 steps! However you need to already have the kudos to burn to do so!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-29",
            "newspiece": "Inpainting is now available on the stable horde! Many kudos to [blueturtle](https://github.com/blueturtleai) for the support!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-25",
            "newspiece": "Another [Discord Bot for Stable Horde integration](https://github.com/ZeldaFan0225/Stable_Horde_Discord) has appeared!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-24",
            "newspiece": "The Stable Horde Client has been renamed to [Lucid Creations](https://dbzer0.itch.io/lucid-creations) and has a new version and UI out which supports multiple models and img2img!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-22",
            "newspiece": "We have [a new npm SDK](https://github.com/ZeldaFan0225/stable_horde) for integrating into the Stable Horde.",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-22",
            "newspiece": "Krita and GIMP plugins now support img2img",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-21",
            "newspiece": "Image 2 Image is now available for everyone!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-20",
            "newspiece": "Stable Diffusion 1.5 is now available!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-17",
            "newspiece": "We now have [a Krita plugin](https://github.com/blueturtleai/krita-stable-diffusion).",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-17",
            "newspiece": "Img2img on the horde is now on pilot for trusted users.",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-16",
            "newspiece": "Yet [another Web UI](https://tinybots.net/artbot) has appeared.",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-11",
            "newspiece": "A [new dedicated Web UI](https://aqualxx.github.io/stable-ui/) has entered the scene!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-10",
            "newspiece": "You can now contribute a worker to the horde [via google colab](https://colab.research.google.com/github/harrisonvanderbyl/ravenbot-ai/blob/master/Horde.ipynb). Just fill-in your API key and run!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-06",
            "newspiece": "We have a [new installation video](https://youtu.be/wJrp5lpByCc) for both the Stable Horde Client and the Stable horde worker.",
            "importance": "Information"
        },
    ]

    def get_news(self):
        return(super().get_news() + self.STABLE_HORDE_NEWS)
