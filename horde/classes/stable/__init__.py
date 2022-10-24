from ..base import *

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
        self.use_gfpgan = params.get("use_gfpgan", False)
        self.use_real_esrgan = params.get("use_real_esrgan", False)
        self.use_ldsr = params.get("use_ldsr", False)
        self.use_upscaling = params.get("use_upscaling", False)
        # To avoid unnecessary calculations, we do it once here.
        self.things = self.width * self.height * self.steps
        # The total amount of to pixelsteps requested.
        self.total_usage = round(self.things * self.n / thing_divisor,2)
        self.source_image = kwargs.get("source_image", None)
        self.models = kwargs.get("models", ['stable_diffusion'])
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
            self.gen_payload["use_gfpgan"] = self.use_gfpgan
            self.gen_payload["use_real_esrgan"] = self.use_real_esrgan
            self.gen_payload["use_ldsr"] = self.use_ldsr
            self.gen_payload["use_upscaling"] = self.use_upscaling
            # if not self.nsfw and self.censor_nsfw:
            #     self.gen_payload["use_nsfw_censor"] = True
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
            prompt_type = "img2img"
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
        '''
        if self.source_image:
            kudos = kudos * 1.5
        super().record_usage(raw_things, kudos)

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


class Worker(Worker):

    def check_in(self, max_pixels, **kwargs):
        super().check_in(**kwargs)
        self.max_pixels = max_pixels
        self.allow_img2img = kwargs.get('allow_img2img', True)
        self.allow_unsafe_ipaddr = kwargs.get('allow_unsafe_ipaddr', True)
        if len(self.models) == 0:
            self.models = ['stable_diffusion']
        paused_string = ''
        if self.paused:
            paused_string = '(Paused) '
        logger.debug(f"{paused_string}Worker {self.name} checked-in, offering models {self.models} at {self.max_pixels} max pixels")

    def calculate_uptime_reward(self):
        return(50)

    def can_generate(self, waiting_prompt):
        can_generate = super().can_generate(waiting_prompt)
        is_matching = can_generate[0]
        skipped_reason = can_generate[1]
        if not is_matching:
            return([is_matching,skipped_reason])
        if self.max_pixels < waiting_prompt.width * waiting_prompt.height:
            is_matching = False
            skipped_reason = 'max_pixels'
        if waiting_prompt.source_image and self.bridge_version < 2:
            is_matching = False
            skipped_reason = 'img2img'
        # These samplers are currently crashing nataili. Disabling them from these workers until we can figure it out
        if waiting_prompt.gen_payload.get('sampler_name', 'k_euler') in ['DDIM', 'PLMS'] and self.bridge_version == 3:
            is_matching = False
            skipped_reason = 'worker_id'
        if waiting_prompt.source_image and not self.allow_img2img:
            is_matching = False
            skipped_reason = 'img2img'
        if not waiting_prompt.safe_ip and not self.allow_unsafe_ipaddr:
            is_matching = False
            skipped_reason = 'unsafe_ip'
        return([is_matching,skipped_reason])

    def get_details(self, is_privileged = False):
        ret_dict = super().get_details(is_privileged)
        ret_dict["max_pixels"] = self.max_pixels
        ret_dict["megapixelsteps_generated"] = self.contributions
        allow_img2img = self.allow_img2img
        if self.bridge_version < 3: allow_img2img = False
        ret_dict["img2img"] = allow_img2img
        return(ret_dict)

    @logger.catch(reraise=True)
    def serialize(self):
        ret_dict = super().serialize()
        ret_dict["max_pixels"] = self.max_pixels
        ret_dict["allow_img2img"] = self.allow_img2img
        ret_dict["allow_unsafe_ipaddr"] = self.allow_unsafe_ipaddr
        return(ret_dict)

    @logger.catch(reraise=True)
    def deserialize(self, saved_dict, convert_flag = None):
        super().deserialize(saved_dict, convert_flag)
        if not self.models or len(self.models) == 0 or None in self.models:
            self.models = ['stable_diffusion']
        self.max_pixels = saved_dict["max_pixels"]
        self.allow_img2img = saved_dict.get("allow_img2img", True)
        self.allow_unsafe_ipaddr = saved_dict.get("allow_unsafe_ipaddr", True)
        if convert_flag == 'pixelsteps':
            self.contributions = round(self.contributions / 50,2)

class Database(Database):

    def convert_things_to_kudos(self, pixelsteps, **kwargs):
        # The baseline for a standard generation of 512x512, 50 steps is 10 kudos
        kudos = round(pixelsteps / (512*512*5),2)
        # logger.info([pixels,multiplier,kudos])
        return(kudos)

    def new_worker(self):
        return(Worker(self))
    def new_user(self):
        return(User(self))
    def new_stats(self):
        return(Stats(self))


class News(News):

    STABLE_HORDE_NEWS = [
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
            "newspiece": "A [new dedicated Web UI](https://aqualxx.github.io/stable-ui/) has enterred the scene!",
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
