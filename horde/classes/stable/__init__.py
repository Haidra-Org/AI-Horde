from ..base import *


class WaitingPrompt(WaitingPrompt):

    def extract_params(self, params):
        self.n = params.pop('n', 1)
        self.steps = params.pop('steps', 50)
        # We assume more than 20 is not needed. But I'll re-evalute if anyone asks.
        if self.n > 20:
            logger.warning(f"User {self.user.get_unique_alias()} requested {self.n} gens per action. Reducing to 20...")
            self.n = 20
        self.width = params.get("width", 512)
        self.height = params.get("height", 512)
        # To avoid unnecessary calculations, we do it once here.
        self.pixelsteps = self.width * self.height * self.steps
        # The total amount of to pixelsteps requested.
        self.total_usage = round(self.pixelsteps * self.n / 1000000,2)
        self.prepare_job_payload(params)

    def prepare_job_payload(self, initial_dict = {}):
        # This is what we send to KoboldAI to the /generate/ API
        self.gen_payload = initial_dict
        self.gen_payload["prompt"] = self.prompt
        # We always send only 1 iteration to KoboldAI
        self.gen_payload["batch_size"] = 1
        self.gen_payload["ddim_steps"] = self.steps

    def activate(self):
        # We separate the activation from __init__ as often we want to check if there's a valid server for it
        # Before we add it to the queue
        super().activate()
        logger.info(f"New prompt by {self.user.get_unique_alias()}: w:{self.width} * h:{self.height} * s:{self.steps} * n:{self.n} == {self.total_usage} Total MPs")

    # The mps still queued to be generated for this WP
    def get_queued_megapixelsteps(self):
        return(round(self.pixelsteps * self.n/1000000,2))

    def get_status(self, lite = False):
        ret_dict = super().get_status(lite)
        queue_pos, queued_mps, queued_n = self.get_own_queue_stats()
        # We increment the priority by 1, because it starts at 0
        # This means when all our requests are currently processing or done, with nothing else in the queue, we'll show queue position 0 which is appropriate.
        ret_dict["queue_position"] = queue_pos + 1
        active_servers = self.db.count_active_servers()
        # If there's less requests than the number of active servers
        # Then we need to adjust the parallelization accordingly
        if queued_n < active_servers:
            active_servers = queued_n
        mpss = (self.db.stats.get_request_avg() / 1000000) * active_servers
        # Is this is 0, it means one of two things:
        # 1. This horde hasn't had any requests yet. So we'll initiate it to 1mpss
        # 2. All gens for this WP are being currently processed, so we'll just set it to 1 to avoid a div by zero, but it's not used anyway as it will just divide 0/1
        if mpss == 0:
            mpss = 1
        wait_time = queued_mps / mpss
        # We add the expected running time of our processing gens
        for procgen in self.processing_gens:
            wait_time += procgen.get_expected_time_left()
        ret_dict["wait_time"] = round(wait_time)
        return(ret_dict)

class ProcessingGeneration(WaitingPrompt):

    def set_generation(self, generation, seed):
        if self.is_completed():
            return(0)
        self.generation = generation
        self.seed = seed
        pixelsteps_per_sec = self.owner.db.stats.record_fulfilment(self.owner.pixelsteps, self.start_time)
        self.kudos = self.owner.db.convert_pixelsteps_to_kudos(self.owner.pixelsteps)
        self.server.record_contribution(self.owner.pixelsteps, self.kudos, pixelsteps_per_sec)
        self.owner.record_usage(self.owner.pixelsteps, self.kudos)
        logger.info(f"New Generation worth {self.kudos} kudos, delivered by server: {self.server.name}")
        return(self.kudos)

    def get_seconds_needed(self):
        return(self.owner.pixelsteps / self.server.get_performance_average())

    def get_details(self):
        '''Returns a dictionary with details about this processing generation'''
        ret_dict = {
            "img": procgen.generation,
            "seed": procgen.seed,
            "server_id": procgen.server.id,
            "server_name": procgen.server.name,
        }
        return(ret_dict)

class KAIServer(KAIServer):
    pass

class PromptsIndex(PromptsIndex):
    pass
               
class GenerationsIndex(GenerationsIndex):
    pass

class User(User):
    pass

class Stats(Stats):
    pass

class Database(Database):

    def convert_pixelsteps_to_kudos(self, pixelsteps):
        # The baseline for a standard generation of 512x512, 50 steps is 10 kudos
        kudos = round(pixelsteps / (512*512*5),2)
        # logger.info([pixels,multiplier,kudos])
        return(kudos)
