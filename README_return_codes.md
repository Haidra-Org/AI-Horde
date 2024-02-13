# AI Horde Errors

Every time an exception is raised by the AI Horde, it will return both the http code, a human-readable "message" and an `rc` field which will contain a unique string code for each type of error the horde might raise. You can use these to better adjust your client logic or for informing your users using your own words or in other languages.

This page will list all existing Horde RCs

## Format

The errors returned by the AI horde are always in this json format

```json
{
  "message": "Human readable explanation on what went wrong",
  "rc": "SomeReturnCodeInCamelCase"
}
```

## Return Codes

| RC | Explanation |
| -- | ----------- |
| MissingPrompt | The generation prompt was not given |
| CorruptPrompt | The prompts was rejected as unethical |
| KudosValidationError | Something went wrong when transferring kudos. This is a base rc, so you should never typically see it. | 
| NoValidActions | Something went wrong when modifying an entity on the horde. This is a base rc, so you should never typically see it. | 
| InvalidSize | Requested image size is not a multiple of 64 |
| InvalidPromptSize | Prompt is too large | 
| TooManySteps | Too many steps requested for image generation | 
| Profanity | Profanity Detected. This is a base rc, so you should never typically see it.
| ProfaneWorkerName | Profanity detected in worker name | 
| ProfaneBridgeAgent | Profanity detected in bridge agent | 
| ProfaneWorkerInfo | Profanity detected in worker info | 
| ProfaneUserName | Profanity detected in username | 
| ProfaneUserContact | Profanity detected in user contact details | 
| ProfaneAdminComment | Profanity detected in admin comment | 
| ProfaneTeamName | Profanity detected in team name | 
| ProfaneTeamInfo | Profanity detected in team info | 
| TooLong | Provided string was too long. This is a base rc, so you should never typically see it. |
| TooLongWorkerName | The provided worker name is too long | 
| TooLongUserName | The provided username is too long | 
| NameAlreadyExists | The provided name already exists. This is a base rc, so you should never typically see it. | 
| WorkerNameAlreadyExists | The provided worker name already exists |
| TeamNameAlreadyExists | The provided team name already exists |
| PolymorphicNameConflict | The provided worker name already exists for a different worker type (e.g. Dreamer VS Scribe) |
| ImageValidationFailed | Source image validation failed unexpectedly |
| SourceImageResolutionExceeded | Source image resolution larger than the max allowed by the AI Horde |
| SourceImageSizeExceeded | Source image file size larger than the max allowed by the AI Horde |
| SourceImageUrlInvalid | Source image url does not contain an image |
| SourceImageUnreadable | Source image could not be parsed |
| InpaintingMissingMask | Missing mask or alpha channel for inpainting |
| SourceMaskUnnecessary | Source mask sent without a source image |
| UnsupportedSampler | Selected sampler unsupported with selected model |
| UnsupportedModel | The required model name is unsupported with this payload. This is a base rc, so you should never typically see it. |
| ControlNetUnsupported | ControlNet is unsupported in combination with this model |
| ControlNetSourceMissing | Missing source image for ControlNet workflow |
| ControlNetInvalidPayload | sent CN source and requested CN source at the same time |
| SourceImageRequiredForModel | Source image is required for using this model |
| UnexpectedModelName | Model name sent is not a Stable Diffusion checkpoint |
| TooManyUpscalers | Tried to use more than 1 upscaler at a time |
| ProcGenNotFound | The used generation for aesthetic ratings doesn't exist |
| InvalidAestheticAttempt | Aesthetics rating attempt failed |
| AestheticsNotCompleted | Attempted to rate non-completed request |
| AestheticsNotPublic | Attempted to rate non-shared request |
| AestheticsDuplicate | Sent duplicate images in an aesthetics set |
| AestheticsMissing | Aesthetic ratings missing |
| AestheticsSolo | Aesthetic ratings best-of contain a single image |
| AestheticsConfused | The best image is not the one with the highest aesthetic rating |
| AestheticsAlreadyExist | Aesthetic rating already submitted |
| AestheticsServerRejected | Aesthetic server rejected submission |
| AestheticsServerError | Aesthetic server returned error (provided) |
| AestheticsServerDown | Aesthetic server is down |
| AestheticsServerTimeout | Aesthetic server timed out during submission |
| InvalidAPIKey | Invalid AI Horde API key provided |
| WrongCredentials | Provided user does not own this worker |
| NotAdmin | Request needs AI Horded admin credentials |
| NotModerator | Request needs AI Horded moderator credentials |
| NotOwner | Request needs worker owner credentials |
| NotPrivileged | This user is not hardcoded to perform this operation |
| AnonForbidden | Anonymous is not allowed to perform this operation |
| AnonForbiddenWorker | Anonymous tried to run a worker |
| AnonForbiddenUserMod | Anonymous tried to modify their user account |
| NotTrusted | Untrusted users are not allowed to perform this operation |
| UntrustedTeamCreation | Untrusted user tried to create a team |
| UntrustedUnsafeIP | Untrusted user tried to use a VPN for a worker |
| WorkerMaintenance | Worker has been put into maintenance and cannot pop new jobs |
| WorkerFlaggedMaintenance | Worker owner has been flagged and worker has been put into permanent maintenance |
| TooManySameIPs | Same IP attempted to spawn too many workers | 
| WorkerInviteOnly | AI Horde is in worker invite-only mode and worker owner needs to request permission |
| UnsafeIP | Worker attempted to connect from VPN |
| TimeoutIP | Operation rejected because user IP in timeout |
| TooManyNewIPs | Too many workers from new IPs currently | 
| KudosUpfront | This request requires upfront kudos to accept |
| SharedKeyEmpty | Shared Key used in the request does not have any more kudos |
| InvalidJobID | Job not found when trying to submit. This probably means its request was delected for inactivity |
| RequestNotFound | Request not found. This probably means it was delected for inactivity |
| WorkerNotFound | Worker ID not found |
| TeamNotFound | Team ID not found |
| FilterNotFound | Regex filter not found | 
| UserNotFound | User not found |
| DuplicateGen | Job has already been submitted |
| AbortedGen | Request aborted because too many jobs have failed |
| RequestExpired | Request expired |
| TooManyPrompts | User has requested too many generations concurrently |
| NoValidWorkers | No workers online which can pick up this request | 
| MaintenanceMode | Request aborted because horde is in maintenance mode |
| TargetAccountFlagged | Action rejected because target user has been flagged for violating Horde ToS |
| SourceAccountFlagged | Action rejected because source user has been flagged for violating Horde ToS |
| FaultWhenKudosReceiving | Unexpected error when receiving kudos | 
| FaultWhenKudosSending | Unexpected error when sending kudos |
| TooFastKudosTransfers | User tried to send kudos too fast after receiving them from the same user | 
| KudosTransferToAnon | User tried to transfer kudos to Anon |
| KudosTransferToSelf | User tried to transfer kudos to themselves | 
| KudosTransferNotEnough | User tried to transfer more kudos than they have |
| NegativeKudosTransfer | User tried to transfer negative kudos |
| KudosTransferFromAnon | User tried to transfer kudos using the Anon API key |
| InvalidAwardUsername | Tried to award kudos to non-existing user | 
| KudosAwardToAnon | Tried to award kudos to Anonymous user | 
| NotAllowedAwards | This user is not allowed to Award Kudos | 
| NoWorkerModSelected | No valid worker modification selected |
| NoUserModSelected | No valid user modification selected |
| NoHordeModSelected | No valid horde modification selected |
| NoTeamModSelected | No valid team modification selected |
| NoFilterModSelected | No valid regex filter modification selected |
| NoSharedKeyModSelected | No valid shared key modification selected |
| BadRequest | Generic HTTP 400 code. You should typically never see this |
| Forbidden | Generic HTTP 401 code. You should typically never see this |
| Locked | Generic HTTP code. You should typically never see this |

