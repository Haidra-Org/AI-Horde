from werkzeug import exceptions as wze
from .. logger import logger

class MissingPrompt(wze.BadRequest):
    def __init__(self, username):
        self.specific = "You cannot specify an empty prompt."
        self.log = f"User '{username}' sent an empty prompt. Aborting!"

class CorruptPrompt(wze.BadRequest):
    def __init__(self, username, ip, prompt, words):
        self.specific = f"This prompt appears to violate our terms of service (blacklisted words: {words}) and will be reported. Please contact us if you think this is an error."
        self.log = f"User '{username}' with IP '{ip}' sent a corrupt prompt '{prompt}' with blacklisted words: {words}. Aborting!"

class KudosValidationError(wze.BadRequest):
    def __init__(self, username, error_message, action = "transfer"):
        self.specific = error_message
        self.log = f"User '{username}' Failed to {action} Kudos."

class NoValidActions(wze.BadRequest):
    def __init__(self, error_message):
        self.specific = error_message
        self.log = None

class InvalidSize(wze.BadRequest):
    def __init__(self, username):
        self.specific = "Invalid size. The image dimensions have to be multiples of 64."
        self.log = f"User '{username}' sent an invalid size. Aborting!"

class InvalidPromptSize(wze.BadRequest):
    def __init__(self, username):
        self.specific = "Too large prompt. Please reduce the amount of tokens contained."
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
    def __init__(self, username, old_name, new_name, object_type = 'worker'):
        self.specific = f"The specified {object_type} name '{new_name}' is already taken!"
        self.log = f"User '{username}' tried to change {object_type} name from {old_name} to {new_name}. Aborting!"

class PolymorphicNameConflict(wze.BadRequest):
    def __init__(self, name, object_type = 'worker'):
        self.specific = f"The specified name '{name}' is already taken by a different type of {object_type}. Please choose a different name!"
        self.log = None

class ImageValidationFailed(wze.BadRequest):
    def __init__(self, message = "Please ensure the source image payload is either a URL containing an image or a valid base64 encoded image."):
        self.specific = f"Image validation failed. {message}"
        self.log = "Source image validation failed"

class SourceMaskUnnecessary(wze.BadRequest):
    def __init__(self):
        self.specific = f"Please do not pass a source_mask unless you are sending a source_image as well"
        self.log = "Tried to pass source_mask with txt2img"

class UnsupportedSampler(wze.BadRequest):
    def __init__(self):
        self.specific = f"This sampler is not supported in this mode the moment"
        self.log = None

class UnsupportedModel(wze.BadRequest):
    def __init__(self):
        self.specific = "This model is not supported in this mode the moment"
        self.log = None

class ProcGenNotFound(wze.BadRequest):
    def __init__(self, procgen_id):
        self.specific = f"Image with ID '{procgen_id}' not found in this request."
        self.log = f"Attempted to log aesthetic rating with non-existent image ID '{procgen_id}'"

class InvalidAestheticAttempt(wze.BadRequest):
    def __init__(self, message):
        self.specific = message
        self.log = None

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

class NotPrivileged(wze.Forbidden):
    def __init__(self, username, message, action):
        self.specific = message
        self.log = f"Non-Privileged user '{username}' tried to take privileged action '{action}'. Aborting!"

class AnonForbidden(wze.Forbidden):
    def __init__(self):
        self.specific = "Anonymous user is forbidden from performing this operation"
        self.log = None

class NotTrusted(wze.Forbidden):
    def __init__(self):
        self.specific = "Only Trusted users are allowed to perform this operation"
        self.log = None

class WorkerMaintenance(wze.Forbidden):
    def __init__(self, maintenance_msg):
        self.specific = maintenance_msg
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

class TimeoutIP(wze.Forbidden):
    def __init__(self, ipaddr, ttl):
        self.specific = f"Due to abuse prevention, your IP address has been put into timeout for {ttl} more seconds. Please try again later, or contact us on discord if you think this was an error."
        self.log = f"Client attempted to generate from {ipaddr} while in {ttl} seconds timeout"

class TooManyNewIPs(wze.Forbidden):
    def __init__(self, ipaddr):
        self.specific = f"We are getting too many new workers from unknown IPs. To prevent abuse, please try again later. If this persists, please contact us on discord https://discord.gg/3DxrhksKzn "
        self.log = f"Too many new IPs to check: {ipaddr}. Asked to retry"

class KudosUpfront(wze.Forbidden):
    def __init__(self, kudos_required, username, res):
        self.specific = f"Due to heavy demand, for requests over {res}x{res} or over 50 steps (25 for k_heun and k_dpm_2*), the client needs to already have the required kudos. This request requires {kudos_required} kudos to fulfil."
        self.log = f"{username} attempted request for {kudos_required} kudos without having enough."

class InvalidJobID(wze.NotFound):
    def __init__(self, job_id):
        self.specific = f"Processing Job with ID {job_id} does not exist."
        self.log = f"Worker attempted to provide job for {job_id} but it did not exist"

class RequestNotFound(wze.NotFound):
    def __init__(self, req_id, request_type = 'Waiting Prompt'):
        self.specific = f"{request_type} with ID '{req_id}' not found."
        if request_type != "Interrogation": #FIXME: Figure out why there's so many
            self.log = f"Status of {request_type} with ID '{req_id}' does not exist"
        else:
            self.log = None

class WorkerNotFound(wze.NotFound):
    def __init__(self, worker_id):
        self.specific = f"Worker with ID '{worker_id}' not found."
        self.log = f"Attempted to retrieve worker with non-existent ID '{worker_id}'"

class TeamNotFound(wze.NotFound):
    def __init__(self, team_id):
        self.specific = f"Team with ID '{team_id}' not found."
        self.log = f"Attempted to retrieve team with non-existent ID '{team_id}'"

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
    def __init__(self, username, count, concurrency):
        self.specific = f"Parallel requests ({count}) exceeded user limit ({concurrency}). Please try again later or request to increase your concurrency."
        self.log = f"User '{username}' has already requested too many parallel requests ({count}/{concurrency}). Aborting!"

class NoValidWorkers(wze.BadRequest):
    retry_after = 600
    def __init__(self, username):
        self.specific = f"No active worker found to fulfill this request. Please Try again later..."
        self.log = f"No active worker found to match the request from '{username}'. Aborting!"

class MaintenanceMode(wze.BadRequest):
    retry_after = 60
    def __init__(self, endpoint):
        self.specific = f"Horde has entered maintenance mode. Please try again later."
        self.log = f"Rejecting endpoint '{endpoint}' because horde in maintenance mode."

class Locked(wze.Locked):
    def __init__(self, message):
        self.specific = message
        self.log = None

def handle_bad_requests(error):
    '''Namespace error handler'''
    if error.log:
        logger.warning(error.log)
    return({'message': error.specific}, error.code)
