from ..base import *

class WaitingPrompt(WaitingPrompt):

    def extract_params(self, params, **kwargs):
        self.n = params.pop('n', 1)
        self.steps = params.pop('steps', 50)
        # We assume more than 20 is not needed. But I'll re-evalute if anyone asks.
        if self.n > 20:
            logger.warning(f"User {self.user.get_unique_alias()} requested {self.n} gens per action. Reducing to 20...")
            self.n = 20
        self.max_length = params.get("max_length", 80)
        self.max_content_length = params.get("max_content_length", 1024)
        # To avoid unnecessary calculations, we do it once here.
        self.things = self.max_length
        # The total amount of to pixelsteps requested.
        self.total_usage = round(self.max_length * self.n / thing_divisor,2)
        self.models = kwargs.get("models", 'ReadOnly')
        self.softprompts = kwargs.get("softprompts", [''])
        self.prepare_job_payload(params)

    def prepare_job_payload(self, initial_dict = {}):
        # This is what we send to KoboldAI to the /generate/ API
        self.gen_payload = initial_dict
        self.gen_payload["prompt"] = self.prompt
        # We always send only 1 iteration to KoboldAI
        self.gen_payload["n"] = 1

    def activate(self):
        # We separate the activation from __init__ as often we want to check if there's a valid worker for it
        # Before we add it to the queue
        super().activate()
        logger.info(f"New prompt by {self.user.get_unique_alias()}: token:{self.max_length} * n:{self.n} == {self.total_usage} Total Tokens")

    def new_procgen(self, worker):
        return(ProcessingGeneration(self, self._processing_generations, worker))

    def start_generation(self, worker, matching_softprompt):
        prompt_payload = super().start_generation(worker)
        prompt_payload["softprompt"] = matching_softprompt
        
        return(prompt_payload)

class ProcessingGeneration(ProcessingGeneration):


    def get_details(self):
        '''Returns a dictionary with details about this processing generation'''
        ret_dict = {
            "text": self.generation,
            "seed": self.seed, # This is not displayed at the moment, but hopefully in the future
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
        }
        return(ret_dict)

class Worker(Worker):

    def check_in(self, max_length, max_content_length, softprompts, **kwargs):
        super().check_in(**kwargs)
        self.max_length = max_length
        self.max_content_length = max_content_length
        self.softprompts = softprompts
        logger.debug(f"Worker {self.name} checked-in, offering model {self.model} at {self.max_length} max tokens and {self.max_content_length} max content length.")

    def calculate_uptime_reward(self):
        return(round(self.db.stats.calculate_model_multiplier(self.model) * 25 / 2.75, 2))

    def can_generate(self, waiting_prompt):
        can_generate = super().can_generate(waiting_prompt)
        is_matching = can_generate[0]
        skipped_reason = can_generate[1]
        if len(waiting_prompt.models) >= 1 and self.model not in waiting_prompt.models:
            logger.debug([len(waiting_prompt.models),self.model,waiting_prompt.models])
            is_matching = False
            skipped_reason = 'models'
        if self.max_content_length < waiting_prompt.max_content_length:
            is_matching = False
            skipped_reason = 'max_content_length'
        if self.max_length < waiting_prompt.max_length:
            is_matching = False
            skipped_reason = 'max_length'
        matching_softprompt = False
        for sp in waiting_prompt.softprompts:
            # If a None softprompts has been provided, we always match, since we can always remove the softprompt
            if sp == '':
                matching_softprompt = True
                break
            for sp_name in self.softprompts:
                if sp in sp_name:
                    matching_softprompt = True
                    break
        if not matching_softprompt:
            is_matching = False
            skipped_reason = 'matching_softprompt'
        return([is_matching,skipped_reason])

    def get_details(self, is_privileged = False):
        ret_dict = super().get_details(is_privileged)
        ret_dict["model"] = self.model
        ret_dict["max_length"] = self.max_length
        ret_dict["max_content_length"] = self.max_content_length
        return(ret_dict)

    @logger.catch
    def serialize(self):
        ret_dict = super().serialize()
        ret_dict["model"] = self.model
        ret_dict["max_length"] = self.max_length
        ret_dict["max_content_length"] = self.max_content_length
        return(ret_dict)

    @logger.catch
    def deserialize(self, saved_dict, convert_flag = None):
        super().deserialize(saved_dict, convert_flag)
        self.model = saved_dict["model"]
        self.max_length = saved_dict["max_length"]
        self.max_content_length = saved_dict["max_content_length"]

class Stats(Stats):

    model_mulitpliers = {}

    def calculate_model_multiplier(self, model_name):
        # To avoid doing this calculations all the time
        multiplier = self.model_mulitpliers.get(model_name)
        if multiplier:
            return(multiplier)
        try:
            import transformers, accelerate
            config = transformers.AutoConfig.from_pretrained(model_name)
            with accelerate.init_empty_weights():
                model = transformers.AutoModelForCausalLM.from_config(config)
            params_sum = sum(v.numel() for v in model.state_dict().values())
            logger.info(f"New Model {model_name} parameter = {params_sum}")
            multiplier = params_sum / 1000000000
        except OSError:
            logger.error(f"Model '{model_name}' not found in hugging face. Defaulting to multiplier of 1.")
            multiplier = 1
        self.model_mulitpliers[model_name] = multiplier
        return(multiplier)


class Database(Database):

    def convert_things_to_kudos(self, tokens, **kwargs):
        logger.debug(kwargs)
        multiplier = self.stats.calculate_model_multiplier(kwargs['model_name'])
        # We want a 2.7B model at 80 tokens to be worth around 10 kudos
        kudos = round(tokens * multiplier / 21, 2)
        return(kudos)

    def new_worker(self):
        return(Worker(self))
    def new_user(self):
        return(User(self))
    def new_stats(self):
        return(Stats(self))


class News(News):

    KOBOLDAI_HORDE_NEWS = [
        {
            "date_published": "2022-10-13",
            "newspiece": "KoboldAI Has been upgraded to the new countermeasures",
            "importance": "Information"
        },
    ]

    def get_news(self):
        return(super().get_news() + self.KOBOLDAI_HORDE_NEWS)
