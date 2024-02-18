from werkzeug import exceptions as wze

from horde.logger import logger

KNOWN_RC = [
    "MissingPrompt",
    "CorruptPrompt",
    "KudosValidationError",
    "NoValidActions",
    "InvalidSize",
    "InvalidPromptSize",
    "TooManySteps",
    "Profanity",
    "ProfaneWorkerName",
    "ProfaneBridgeAgent",
    "ProfaneWorkerInfo",
    "ProfaneUserName",
    "ProfaneUserContact",
    "ProfaneAdminComment",
    "ProfaneTeamName",
    "ProfaneTeamInfo",
    "TooLong",
    "TooLongWorkerName",
    "TooLongUserName",
    "NameAlreadyExists",
    "WorkerNameAlreadyExists",
    "TeamNameAlreadyExists",
    "PolymorphicNameConflict",
    "ImageValidationFailed",
    "SourceImageResolutionExceeded",
    "SourceImageSizeExceeded",
    "SourceImageUrlInvalid",
    "SourceImageUnreadable",
    "InpaintingMissingMask",
    "SourceMaskUnnecessary",
    "UnsupportedSampler",
    "UnsupportedModel",
    "ControlNetUnsupported",
    "ControlNetSourceMissing",
    "ControlNetInvalidPayload",
    "SourceImageRequiredForModel",
    "UnexpectedModelName",
    "TooManyUpscalers",
    "ProcGenNotFound",
    "InvalidAestheticAttempt",
    "AestheticsNotCompleted",
    "AestheticsNotPublic",
    "AestheticsDuplicate",
    "AestheticsMissing",
    "AestheticsSolo",
    "AestheticsConfused",
    "AestheticsAlreadyExist",
    "AestheticsServerRejected",
    "AestheticsServerError",
    "AestheticsServerDown",
    "AestheticsServerTimeout",
    "InvalidAPIKey",
    "WrongCredentials",
    "NotAdmin",
    "NotModerator",
    "NotOwner",
    "NotPrivileged",
    "AnonForbidden",
    "AnonForbiddenWorker",
    "AnonForbiddenUserMod",
    "NotTrusted",
    "UntrustedTeamCreation",
    "UntrustedUnsafeIP",
    "WorkerMaintenance",
    "WorkerFlaggedMaintenance",
    "TooManySameIPs",
    "WorkerInviteOnly",
    "UnsafeIP",
    "TimeoutIP",
    "TooManyNewIPs",
    "KudosUpfront",
    "SharedKeyEmpty",
    "InvalidJobID",
    "RequestNotFound",
    "WorkerNotFound",
    "TeamNotFound",
    "FilterNotFound",
    "UserNotFound",
    "DuplicateGen",
    "AbortedGen",
    "RequestExpired",
    "TooManyPrompts",
    "NoValidWorkers",
    "MaintenanceMode",
    "TargetAccountFlagged",
    "SourceAccountFlagged",
    "FaultWhenKudosReceiving",
    "FaultWhenKudosSending",
    "TooFastKudosTransfers",
    "KudosTransferToAnon",
    "KudosTransferToSelf",
    "KudosTransferNotEnough",
    "NegativeKudosTransfer",
    "KudosTransferFromAnon",
    "InvalidAwardUsername",
    "KudosAwardToAnon",
    "NotAllowedAwards",
    "NoWorkerModSelected",
    "NoUserModSelected",
    "NoHordeModSelected",
    "NoTeamModSelected",
    "NoFilterModSelected",
    "NoSharedKeyModSelected",
    "BadRequest",
    "Forbidden",
    "Locked",
    "ControlNetMismatch",
    "HiResFixMismatch",
    "TooManyLoras",
    "BadLoraVersion",
    "TooManyTIs",
    "BetaAnonForbidden",
    "BetaComparisonFault",
    "BadCFGDecimals",
    "BadCFGNumber",
    "BadClientAgent",
    "SpecialMissingPayload",
    "SpecialForbidden",
    "SpecialMissingUsername",
    "SpecialModelNeedsSpecialUser",
    "SpecialFieldNeedsSpecialUser",
]


class BadRequest(wze.BadRequest):
    def __init__(self, message, log=None, rc="BadRequest"):
        self.specific = message
        self.log = log
        self.rc = rc


class Forbidden(wze.Forbidden):
    def __init__(self, message, log=None, rc="Forbidden"):
        self.specific = message
        self.log = log
        self.rc = rc


class Locked(wze.Locked):
    def __init__(self, message, rc="Locked"):
        self.specific = message
        self.log = None
        self.rc = rc


class MissingPrompt(wze.BadRequest):
    def __init__(self, username, rc="MissingPrompt"):
        self.specific = "You cannot specify an empty prompt."
        self.log = f"User '{username}' sent an empty prompt. Aborting!"
        self.rc = rc


class CorruptPrompt(wze.BadRequest):
    def __init__(self, username, ip, prompt, message=None, rc="CorruptPrompt"):
        if message:
            self.specific = message
        else:
            self.specific = (
                "This prompt appears to violate our terms of service and will be reported. "
                "Please contact us if you think this is an error."
            )
        self.log = f"User '{username}' with IP '{ip}' sent an a corrupt prompt: '{prompt}'. Aborting!"
        self.rc = rc


class KudosValidationError(wze.BadRequest):
    def __init__(self, username, error_message, action="transfer", rc="KudosValidationError"):
        self.specific = error_message
        self.log = f"User '{username}' Failed to {action} Kudos."
        self.rc = rc


class NoValidActions(wze.BadRequest):
    def __init__(self, error_message, rc="NoValidActions"):
        self.specific = error_message
        self.log = None
        self.rc = rc


class InvalidSize(wze.BadRequest):
    def __init__(self, username, rc="InvalidSize"):
        self.specific = "Invalid size. The image dimensions have to be multiples of 64."
        self.log = f"User '{username}' sent an invalid size. Aborting!"
        self.rc = rc


class InvalidPromptSize(wze.BadRequest):
    def __init__(self, username, rc="InvalidPromptSize"):
        self.specific = "Too large prompt. Please reduce the amount of tokens contained."
        self.log = f"User '{username}' sent an invalid size. Aborting!"
        self.rc = rc


class TooManySteps(wze.BadRequest):
    def __init__(self, username, steps, rc="TooManySteps"):
        self.specific = "Too many sampling steps. To allow resources for everyone, we allow only up to 500 steps."
        self.log = f"User '{username}' sent too many steps ({steps}). Aborting!"
        self.rc = rc


class Profanity(wze.BadRequest):
    def __init__(self, username, text, text_type, rc="Profanity"):
        self.specific = f"As our API is public, we do not allow profanity in the {text_type}, please try again."
        self.log = f"User '{username}' tried to submit profanity for {text_type} ({text}). Aborting!"
        self.rc = rc


class TooLong(wze.BadRequest):
    def __init__(self, username, chars, limit, text_type, rc="TooLong"):
        self.specific = f"The specified {text_type} is too long. Please stay below {limit}"
        self.log = f"User '{username}' tried to submit {chars} chars for {text_type}. Aborting!"
        self.rc = rc


class NameAlreadyExists(wze.BadRequest):
    def __init__(self, username, old_name, new_name, object_type="worker", rc="NameAlreadyExists"):
        self.specific = f"The specified {object_type} name '{new_name}' is already taken!"
        self.log = f"User '{username}' tried to change {object_type} name from {old_name} to {new_name}. Aborting!"
        self.rc = rc


class PolymorphicNameConflict(wze.BadRequest):
    def __init__(self, name, object_type="worker", rc="PolymorphicNameConflict"):
        self.specific = (
            f"The specified name '{name}' is already taken by a different type " f"of {object_type}. Please choose a different name!"
        )
        self.log = None
        self.rc = rc


class ImageValidationFailed(wze.BadRequest):
    def __init__(
        self,
        message=("Please ensure the source image payload is either " "a URL containing an image or a valid base64 encoded image."),
        rc="ImageValidationFailed",
    ):
        self.specific = f"Image validation failed. {message}"
        self.log = "Source image validation failed"
        self.rc = rc


class SourceMaskUnnecessary(wze.BadRequest):
    def __init__(self, rc="SourceMaskUnnecessary"):
        self.specific = "Please do not pass a source_mask unless you are sending a source_image as well"
        self.log = "Tried to pass source_mask with txt2img"
        self.rc = rc


class UnsupportedSampler(wze.BadRequest):
    def __init__(self, rc="UnsupportedSampler"):
        self.specific = "This sampler is not supported in this mode the moment"
        self.log = None
        self.rc = rc


class UnsupportedModel(wze.BadRequest):
    def __init__(self, message=None, rc="UnsupportedModel"):
        if message:
            self.specific = message
        else:
            self.specific = "This model is not supported in this mode the moment"
        self.log = None
        self.rc = rc


class ProcGenNotFound(wze.BadRequest):
    def __init__(self, procgen_id, rc="ProcGenNotFound"):
        self.specific = f"Image with ID '{procgen_id}' not found in this request."
        self.log = f"Attempted to log aesthetic rating with non-existent image ID '{procgen_id}'"
        self.rc = rc


class InvalidAestheticAttempt(wze.BadRequest):
    def __init__(self, message, rc="InvalidAestheticAttempt"):
        self.specific = message
        self.log = None
        self.rc = rc


class InvalidAPIKey(wze.Unauthorized):
    def __init__(self, subject, keytype="API", rc="InvalidAPIKey"):
        if keytype == "Shared":
            self.specific = "No user matching sent Shared Key."
        else:
            self.specific = "No user matching sent API Key. Have you remembered to register at https://stablehorde.net/register ?"
        self.log = f"Invalid {keytype} Key sent for {subject}"
        self.rc = rc


class WrongCredentials(wze.Forbidden):
    def __init__(self, username, worker, rc="WrongCredentials"):
        self.specific = "Wrong credentials to submit as this worker."
        self.log = f"User '{username}' sent wrong credentials for utilizing worker {worker}"
        self.rc = rc


class NotAdmin(wze.Forbidden):
    def __init__(self, username, endpoint, rc="NotAdmin"):
        self.specific = "You're not an admin. Sod off!"
        self.log = f"Non-admin user '{username}' tried to use admin endpoint: '{endpoint}. Aborting!"
        self.rc = rc


class NotModerator(wze.Forbidden):
    def __init__(self, username, endpoint, rc="NotModerator"):
        self.specific = "You're not a mod. BTFO!"
        self.log = f"Non-mod user '{username}' tried to use mod endpoint: '{endpoint}. Aborting!"
        self.rc = rc


class NotOwner(wze.Forbidden):
    def __init__(self, username, worker_name, rc="NotOwner"):
        self.specific = "You're not an admin. Sod off!"
        self.log = f"User '{username}'' tried to modify worker they do not own '{worker_name}'. Aborting!"
        self.rc = rc


class NotPrivileged(wze.Forbidden):
    def __init__(self, username, message, action, rc="NotPrivileged"):
        self.specific = message
        self.log = f"Non-Privileged user '{username}' tried to take privileged action '{action}'. Aborting!"
        self.rc = rc


class AnonForbidden(wze.Forbidden):
    def __init__(self, rc="AnonForbidden"):
        self.specific = "Anonymous user is forbidden from performing this operation"
        self.log = None
        self.rc = rc


class NotTrusted(wze.Forbidden):
    def __init__(self, rc="NotTrusted"):
        self.specific = "Only Trusted users are allowed to perform this operation"
        self.log = None
        self.rc = rc


class WorkerMaintenance(wze.Forbidden):
    def __init__(self, maintenance_msg, rc="WorkerMaintenance"):
        self.specific = maintenance_msg
        self.log = None
        self.rc = rc


class TooManySameIPs(wze.Forbidden):
    def __init__(self, username, rc="TooManySameIPs"):
        self.specific = (
            "You are running too many workers from the same location. To prevent abuse, "
            "please contact us on Discord to allow you to join more workers from the same IP: https://discord.gg/aG68kk3Qpz"
        )
        self.log = f"User '{username} is trying to onboard too many workers from the same IP Address. Aborting!"
        self.rc = rc


class WorkerInviteOnly(wze.Forbidden):
    def __init__(self, current_workers, rc="WorkerInviteOnly"):
        if current_workers == 0:
            self.specific = (
                "This horde has been switched to worker invite-only mode. "
                "Please contact us on Discord to allow you to join your worker: https://discord.gg/aG68kk3Qpz "
            )
        else:
            self.specific = (
                "This horde has been switched to worker invite-only mode and "
                f"you already have {current_workers} workers. "
                "Please contact us on Discord to allow you to join more workers: https://discord.gg/aG68kk3Qpz "
            )
        self.log = None
        self.rc = rc


class UnsafeIP(wze.Forbidden):
    def __init__(self, ipaddr, rc="UnsafeIP"):
        self.specific = (
            "Due to abuse prevention, we cannot accept more workers from VPNs. "
            "Please contact us on Discord if you feel this is a mistake."
        )
        self.log = f"Worker attempted to pop from unsafe IP: {ipaddr}"
        self.rc = rc


class TimeoutIP(wze.Forbidden):
    def __init__(self, ipaddr, ttl, connect_type="Client", rc="TimeoutIP"):
        base_message = (
            "Due to abuse prevention, your IP address has been put into timeout for {ttl} more seconds. "
            "Please try again later, or contact us on discord if you think this was an error."
        )
        non_atomic_message = (
            "Due to abuse prevention, your IP address has been put into timeout. "
            "Please try again later, or contact us on discord if you think this was an error."
        )

        try:
            ttl = int(ttl)
        except ValueError:
            logger.warning(f"Invalid TTL value: {ttl} during timeout IP for {ipaddr}")
            ttl = None

        if ttl is None or ttl > (60 * 60 * 24 * 4):
            self.specific = non_atomic_message
        else:
            self.specific = base_message.format(ttl=ttl)

        self.log = f"{connect_type} attempted to connect from {ipaddr} while in {ttl} seconds timeout"
        self.rc = rc


class TooManyNewIPs(wze.Forbidden):
    def __init__(self, ipaddr, rc="TooManyNewIPs"):
        self.specific = (
            "We are getting too many new workers from unknown IPs. To prevent abuse, please try again later. "
            "If this persists, please contact us on discord https://discord.gg/3DxrhksKzn"
        )
        self.log = f"Too many new IPs to check: {ipaddr}. Asked to retry"
        self.rc = rc


class KudosUpfront(wze.Forbidden):
    def __init__(self, kudos_required, username, message, rc="KudosUpfront"):
        self.specific = message
        self.log = f"{username} attempted request for {kudos_required} kudos without having enough."
        self.rc = rc


class InvalidJobID(wze.NotFound):
    def __init__(self, job_id, rc="InvalidJobID"):
        self.specific = f"Processing Job with ID {job_id} does not exist."
        self.log = f"Worker attempted to provide job for {job_id} but it did not exist"
        self.rc = rc


class RequestNotFound(wze.NotFound):
    def __init__(
        self,
        req_id,
        request_type="Waiting Prompt",
        client_agent="unknown",
        ipaddr="unknown",
        rc="RequestNotFound",
    ):
        self.specific = f"{request_type} with ID '{req_id}' not found."
        if request_type != "Interrogation":  # FIXME: Figure out why there's so many
            self.log = f"{request_type} with ID '{req_id}' does not exist. Client agent: {client_agent}@{ipaddr}"
        else:
            self.log = None
        self.rc = rc


class WorkerNotFound(wze.NotFound):
    def __init__(self, worker_id, rc="WorkerNotFound"):
        self.specific = f"Worker with ID '{worker_id}' not found."
        self.log = f"Attempted to retrieve worker with non-existent ID '{worker_id}'"
        self.rc = rc


class TeamNotFound(wze.NotFound):
    def __init__(self, team_id, rc="TeamNotFound"):
        self.specific = f"Team with ID '{team_id}' not found."
        self.log = f"Attempted to retrieve team with non-existent ID '{team_id}'"
        self.rc = rc


class ThingNotFound(wze.NotFound):
    def __init__(self, thing_type, thing_id, message=None, rc="ThingNotFound"):
        if message is not None:
            self.specific = message
        else:
            self.specific = f"{thing_type.capitalize()} with ID '{thing_id}' not found."
        self.log = f"Attempted to retrieve {thing_type} with non-existent ID '{thing_id}'"
        self.rc = f"{thing_type.capitalize()}NotFound"


class UserNotFound(wze.NotFound):
    def __init__(self, user_id, lookup_type="ID", message=None, rc="UserNotFound"):
        if message:
            self.specific = message
        else:
            self.specific = f"User with {lookup_type} '{user_id}' not found."
        self.log = f"Attempted to retrieve user with non-existent {lookup_type} '{user_id}'"
        self.rc = rc


class DuplicateGen(wze.BadRequest):
    def __init__(self, worker, gen_id, rc="DuplicateGen"):
        self.specific = f"Processing Generation with ID {gen_id} already submitted."
        self.log = f"Worker '{worker}' attempted to provide duplicate generation for {gen_id}"
        self.rc = rc


class AbortedGen(wze.BadRequest):
    def __init__(self, worker, gen_id, rc="AbortedGen"):
        self.specific = (
            f"Processing Generation with ID {gen_id} took too long to process and has been aborted! "
            "Please check your worker speed and do not onboard worker which generate slower than 1 it/s!"
        )
        self.log = f"Worker '{worker}' attempted to provide aborted generation for {gen_id}."
        self.rc = rc


class RequestExpired(wze.Gone):
    def __init__(self, username, rc="RequestExpired"):
        self.specific = "Prompt Request Expired"
        self.log = f"Request from '{username}' took too long to complete and has been cancelled."
        self.rc = rc


class TooManyPrompts(wze.TooManyRequests):
    def __init__(self, username, count, concurrency, msg=None, rc="TooManyPrompts"):
        if msg is None:
            self.specific = (
                f"Parallel requests ({count}) exceeded user limit ({concurrency}). "
                "Please try again later or request to increase your concurrency."
            )
        else:
            self.specific = msg
        self.log = f"User '{username}' has already requested too many parallel requests ({count}/{concurrency}). Aborting!"
        self.rc = rc


class NoValidWorkers(wze.BadRequest):
    retry_after = 600

    def __init__(self, username, rc="NoValidWorkers"):
        self.specific = "No active worker found to fulfill this request. Please Try again later..."
        self.log = f"No active worker found to match the request from '{username}'. Aborting!"
        self.rc = rc


class MaintenanceMode(wze.BadRequest):
    retry_after = 60

    def __init__(self, endpoint, rc="MaintenanceMode"):
        self.specific = "Horde has entered maintenance mode. Please try again later."
        self.log = f"Rejecting endpoint '{endpoint}' because horde in maintenance mode."
        self.rc = rc


def handle_bad_requests(error):
    """Namespace error handler"""
    if error.log:
        logger.warning(f"{error.rc}: {error.log}")
    return (
        {
            "message": error.specific,
            "rc": error.rc,
        },
        error.code,
    )
