from werkzeug import exceptions as wze
from ..logger import logger

class MissingPrompt(wze.BadRequest):
    def __init__(self, username):
        self.specific = "You cannot specify an empty prompt."
        self.log = f"User '{username}' sent an empty prompt. Aborting!"

class KudosValidationError(wze.BadRequest):
    def __init__(self, username, error_message):
        self.specific = error_message
        self.log = f"User '{username}' Failed to transfer Kudos."

class NoValidActions(wze.BadRequest):
    def __init__(self, error_message):
        self.specific = error_message
        self.log = None

class InvalidSize(wze.BadRequest):
    def __init__(self, username):
        self.specific = "Invalid size. The image dimentions have to be multiples of 64."
        self.log = f"User '{username}' sent an invalid size. Aborting!"

class TooManySteps(wze.BadRequest):
    def __init__(self, username, steps):
        self.specific = "Too many sampling steps. To allow resources for everyone, we allow only up to 100 steps."
        self.log = f"User '{username}' sent too many steps ({steps}). Aborting!"

class InvalidAPIKey(wze.Unauthorized):
    def __init__(self, subject):
        self.specific = "No user matching sent API Key. Have you remembered to register at https://stablehorde.net/register ?"
        self.log = f"Invalid API Key sent for {subject}"

class WrongCredentials(wze.Forbidden):
    def __init__(self, username, worker):
        self.specific = "You cannot specify an empty prompt."
        self.log = f"User '{username}' sent wrong credentials for utilizing worker {worker}"

class NotAdmin(wze.Forbidden):
    def __init__(self, username, endpoint):
        self.specific = "You're not an admin. Sod off!"
        self.log = f"Non-admin user '{username}' tried to use admin endpoint: '{endpoint}. Aborting!"

class NotOwner(wze.Forbidden):
    def __init__(self, username, worker_name):
        self.specific = "You're not an admin. Sod off!"
        self.log = f"User '{username}'' tried to modify worker they do not own '{worker_name}'. Aborting!"

class WorkerMaintenance(wze.Forbidden):
    def __init__(self):
        self.specific = "This worker has been put into maintenance by its owner"
        self.log = None

class InvalidProcGen(wze.NotFound):
    def __init__(self, gen_id):
        self.specific = f"Processing Generation with ID {gen_id} does not exist."
        self.log = f"Worker attempted to provide generation for {gen_id} but it did not exist"

class RequestNotFound(wze.NotFound):
    def __init__(self, wp_id):
        self.specific = f"Request with ID '{wp_id}' not found."
        self.log = f"Status of WP with ID '{wp_id}' does not exist"

class WorkerNotFound(wze.NotFound):
    def __init__(self, worker_id):
        self.specific = f"Worker with ID '{worker_id}' not found."
        self.log = f"Attempted to retrieve worker with non-existent ID '{worker_id}'"

class UserNotFound(wze.NotFound):
    def __init__(self, user_id):
        self.specific = f"User with ID '{user_id}' not found."
        self.log = f"Attempted to retrieve user with non-existent ID '{user_id}'"

class DuplicateGen(wze.NotFound):
    def __init__(self, worker, gen_id):
        self.specific = f"Processing Generation with ID {gen_id} already submitted."
        self.log = f"Worker '{worker}' attempted to provide duplicate generation for {gen_id}"

class RequestExpired(wze.Gone):
    def __init__(self, username):
        self.specific = f"Prompt Request Expired"
        self.log = f"Request from '{username}' took too long to complete and has been cancelled."

class TooManyPrompts(wze.TooManyRequests):
    def __init__(self, username, count):
        self.specific = f"Parallel requests exceeded user limit ({count}). Please try again later or request to increase your concurrency."
        self.log = f"User '{username}' has already requested too many parallel requests ({count}). Aborting!"

class NoValidWorkers(wze.ServiceUnavailable):
    retry_after = 600
    def __init__(self, username):
        self.specific = f"No active worker found to fulfill this request. Please Try again later..."
        self.log = f"No active worker found to match the request from '{username}'. Aborting!"

class MaintenanceMode(wze.ServiceUnavailable):
    retry_after = 60
    def __init__(self, endpoint):
        self.specific = f"Horde has enterred maintenance mode. Please try again later."
        self.log = f"Rejecting endpoint '{endpoint}' because horde in maintenance mode."


def handle_bad_requests(error):
    '''Namespace error handler'''
    if error.log:
        logger.warning(error.log)
    return({'message': error.specific}, error.code)
