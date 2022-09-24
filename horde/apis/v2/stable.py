from . import v2
from .v2 import *

# generate_parser.add_argument("models", type=str, action='append', required=False, default=[], help="Models", location="json")
# generate_parser.add_argument("test", type=str, required=True, help="Models", location="json")

class AsyncGenerate(AsyncGenerateTemplate):
    
    def validate(self):
        super().validate()
        if self.args["params"].get("length",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("width",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("steps",50) > 100:
            raise e.TooManySteps(self.username, self.args['params']['steps'])


class SyncGenerate(SyncGenerateTemplate):
    
    def validate(self):
        super().validate()
        if self.args["params"].get("length",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("width",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("steps",50) > 100:
            raise e.TooManySteps(self.username, self.args['params']['steps'])


api.add_resource(SyncGenerate, "/generate/sync")
api.add_resource(AsyncGenerate, "/generate/async")
api.add_resource(AsyncStatus, "/generate/status/<string:id>")
api.add_resource(AsyncCheck, "/generate/check/<string:id>")
api.add_resource(PromptPop, "/generate/pop")
api.add_resource(SubmitGeneration, "/generate/submit")
api.add_resource(Users, "/users")
api.add_resource(UserSingle, "/users/<string:user_id>")
api.add_resource(Workers, "/workers")
api.add_resource(WorkerSingle, "/workers/<string:worker_id>")
api.add_resource(TransferKudos, "/kudos/transfer")
api.add_resource(HordeLoad, "/status/performance")
api.add_resource(HordeMaintenance, "/status/maintenance")
