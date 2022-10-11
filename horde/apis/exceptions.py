from werkzeug import exceptions as wze
from .. logger import logger

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

class Profanity(wze.BadRequest):
    def __init__(self, username, text, text_type):
        self.specific = f"As our API is public, we do not allow profanity in the {text_type}, please try again."
        self.log = f"User '{username}' tried to submit profanity for {text_type} ({text}). Aborting!"

class TooLong(wze.BadRequest):
    def __init__(self, username, chars, limit, text_type):
        self.specific = f"The specified {text_type} is too long. Please stay below {limit}"
        self.log = f"User '{username}' tried to submit {chars} chars for {text_type}. Aborting!"

class NameAlreadyExists(wze.BadRequest):
    def __init__(self, username, old_worker_name, new_worker_name):
        self.specific = f"The specified worker name '{new_worker_name}' is already taken!"
        self.log = f"User '{username}' tried to change worker name from {old_worker_name} to {new_worker_name}. Aborting!"

class InvalidAPIKey(wze.Unauthorized):
    def __init__(self, subject):
        self.specific = "No user matching sent API Key. Have you remembered to register at https://stablehorde.net/register ?"
        self.log = f"Invalid API Key sent for {subject}"

class WrongCredentials(wze.Forbidden):
    def __init__(self, username, worker):
        self.specific = "Wrong credentials to submit as this worker."
        self.log = f"User '{username}' sent wrong credentials for utilizing worker {worker}"

class NotAdmin(wze.Forbidden):
    def __init__(self, username, endpoint):
        self.specific = "You're not an admin. Sod off!"
        self.log = f"Non-admin user '{username}' tried to use admin endpoint: '{endpoint}. Aborting!"

class NotModerator(wze.Forbidden):
    def __init__(self, username, endpoint):
        self.specific = "You're not a mod. BTFO!"
        self.log = f"Non-mod user '{username}' tried to use mod endpoint: '{endpoint}. Aborting!"

class NotOwner(wze.Forbidden):
    def __init__(self, username, worker_name):
        self.specific = "You're not an admin. Sod off!"
        self.log = f"User '{username}'' tried to modify worker they do not own '{worker_name}'. Aborting!"

class AnonForbidden(wze.Forbidden):
    def __init__(self):
        self.specific = "Anonymous user is forbidden from performing this operation"
        self.log = None

class WorkerMaintenance(wze.Forbidden):
    def __init__(self, worker_id):
        self.specific = f"worker {worker_id} has been put into maintenance by its owner"
        self.log = None

class TooManySameIPs(wze.Forbidden):
    def __init__(self, username):
        self.specific = f"You are running too many workers from the same location. To prevent abuse, please contact us on Discord to allow you to join more workers from the same IP: https://discord.gg/aG68kk3Qpz "
        self.log = f"User '{username} is trying to onboard too many workers from the same IP Address. Aborting!"

class WorkerInviteOnly(wze.Forbidden):
    def __init__(self, current_workers):
        if current_workers == 0:
            self.specific = f"This horde has been switched to worker invite-only mode. Please contact us on Discord to allow you to join your worker: https://discord.gg/aG68kk3Qpz "
        else:
            self.specific = f"This horde has been switched to worker invite-only mode and you already have {current_workers} workers. Please contact us on Discord to allow you to join more workers: https://discord.gg/aG68kk3Qpz "
        self.log = None

class UnsafeIP(wze.Forbidden):
    def __init__(self, ipaddr):
        self.specific = f"Due to abuse prevention, we cannot accept more workers from your IP address. Please contact us on Discord if you feel this is a mistake."
        self.log = f"Worker attempted to pop from unsafe IP: {ipaddr}"

class TooManyNewIPs(wze.Forbidden):
    def __init__(self, ipaddr):
        self.specific = f"We are getting too many new workers from unknown IPs. To prevent abuse, please try again later. If this persists, please contact us on discord https://discord.gg/3DxrhksKzn "
        self.log = f"Too many new IPs to check: {ipaddr}. Asked to retry"

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
    def __init__(self, user_id, lookup_type = 'ID'):
        self.specific = f"User with {lookup_type} '{user_id}' not found."
        self.log = f"Attempted to retrieve user with non-existent {lookup_type} '{user_id}'"

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
