from .. import logger, args
from .server_classes import WaitingPrompt,ProcessingGeneration,KAIServer,PromptsIndex,GenerationsIndex,User,Database

db = Database(convert_flag=args.convert_flag)
waiting_prompts = PromptsIndex()
processing_generations = GenerationsIndex()
