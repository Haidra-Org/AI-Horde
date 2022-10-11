from ..base import *

class WaitingPrompt(WaitingPrompt):

    def extract_params(self, params, **kwargs):
        self.n = params.pop('n', 1)
        self.steps = params.pop('steps', 50)
        # We assume more than 20 is not needed. But I'll re-evalute if anyone asks.
        if self.n > 20:
            logger.warning(f"User {self.user.get_unique_alias()} requested {self.n} gens per action. Reducing to 20...")
            self.n = 20
        self.width = params.get("width", 512)
        self.height = params.get("height", 512)
        # To avoid unnecessary calculations, we do it once here.
        self.things = self.width * self.height * self.steps
        # The total amount of to pixelsteps requested.
        self.total_usage = round(self.things * self.n / thing_divisor,2)
        self.censor_nsfw = kwargs.get("censor_nsfw", True)
        self.seed = None
        if 'seed' in params:
            self.seed = params.pop('seed')
        self.seed_variation = None
        self.generations_done = 0
        if "seed_variation" in params:
            self.seed_variation = params.pop("seed_variation")

        self.prepare_job_payload(params)

    def prepare_job_payload(self, initial_dict = {}):
        # This is what we send to KoboldAI to the /generate/ API
        self.gen_payload = initial_dict
        self.gen_payload["prompt"] = self.prompt
        # We always send only 1 iteration to Stable Diffusion
        self.gen_payload["batch_size"] = 1
        self.gen_payload["ddim_steps"] = self.steps
        self.gen_payload["seed"] = self.seed
        if not self.nsfw and self.censor_nsfw:
            if "toggles" not in self.gen_payload:
                self.gen_payload["toggles"] = [1, 4, 8]
            elif 8 not in self.gen_payload["toggles"]:
                self.gen_payload["toggles"].append(8)

    def get_job_payload(self):
        if self.seed_variation and self.generations_done > 0:
            self.gen_payload["seed"] += self.seed_variation
            while self.gen_payload["seed"] >= 2**32:
                self.gen_payload["seed"] = self.gen_payload["seed"] >> 32
        else:
            self.gen_payload["seed"] = self.seed_to_int(self.seed)
            self.generations_done += 1
        return(self.gen_payload)

    def activate(self):
        # We separate the activation from __init__ as often we want to check if there's a valid worker for it
        # Before we add it to the queue
        super().activate()
        logger.info(f"New prompt by {self.user.get_unique_alias()}: w:{self.width} * h:{self.height} * s:{self.steps} * n:{self.n} == {self.total_usage} Total MPs")

    def new_procgen(self, worker):
        return(ProcessingGeneration(self, self._processing_generations, worker))

    def seed_to_int(self, s = None):
        if type(s) is int:
            return s
        if s is None or s == '':
            return random.randint(0, 2**32 - 1)
        n = abs(int(s) if s.isdigit() else random.Random(s).randint(0, 2**32 - 1))
        while n >= 2**32:
            n = n >> 32
        return n

class ProcessingGeneration(ProcessingGeneration):

    def get_details(self):
        '''Returns a dictionary with details about this processing generation'''
        ret_dict = {
            "img": self.generation,
            "seed": self.seed,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
        }
        return(ret_dict)


class Worker(Worker):

    def check_in(self, max_pixels, **kwargs):
        super().check_in(**kwargs)
        self.max_pixels = max_pixels
        paused_string = ''
        if self.paused:
            paused_string = '(Paused) '
        logger.debug(f"{paused_string}Worker {self.name} checked-in, offering {self.max_pixels} max pixels")

    def calculate_uptime_reward(self):
        return(50)

    def can_generate(self, waiting_prompt):
        can_generate = super().can_generate(waiting_prompt)
        is_matching = can_generate[0]
        skipped_reason = can_generate[1]
        if self.max_pixels < waiting_prompt.width * waiting_prompt.height:
            is_matching = False
            skipped_reason = 'max_pixels'
        return([is_matching,skipped_reason])

    def get_details(self, is_privileged = False):
        ret_dict = super().get_details(is_privileged)
        ret_dict["max_pixels"] = self.max_pixels
        ret_dict["megapixelsteps_generated"] = self.contributions
        return(ret_dict)

    @logger.catch
    def serialize(self):
        ret_dict = super().serialize()
        ret_dict["max_pixels"] = self.max_pixels
        return(ret_dict)

    @logger.catch
    def deserialize(self, saved_dict, convert_flag = None):
        super().deserialize(saved_dict, convert_flag)
        self.max_pixels = saved_dict["max_pixels"]
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
            "date_published": "2022-10-11",
            "newspiece": "A [new dedicated Web UI](https://aqualxx.github.io/stable-ui/) has enterred the scene!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-10",
            "newspiece": "The [discord rewards bot](https://www.patreon.com/posts/new-kind-of-73097166) has been unleashed. Reward good contributions to the horde directly from the chat!",
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
