# KoboldAI Horde

<img style="float:right" src="{horde_img_url}/{horde_image}.jpg" width="300" /> This is a [crowdsourced distributed cluster](https://github.com/db0/AI-Horde) of [KoboldAI clients](https://github.com/KoboldAI/KoboldAI-Client). You can download the client and use it to experience stories similar to AI-Dungeon for free. If you like this service, consider joining the horde yourself!

Also check out our sister project for image generation: [Stable Horde](https://stablehorde.net)


## Stats 

* Average Recent Performance: {avg_performance} {avg_thing_name} per second
* Total tokens generated: {total_things} {total_things_name}
* Total requests fulfilled: {total_fulfillments}{total_fulfillments_char}
* Active [Workers](/api/v2/workers): {active_workers}
* Queue: {total_queue} requests for a total of {queued_things} {queued_things_name}

## Usage

First [Register an account](/register) which will generate for you an API key. Store that key somewhere.

   * if you do not want to register, you can use '0000000000' as api_key to connect anonymously. However anonymous accounts have the lowest priority when there's too many concurrent requests!
   * To increase your priority you will need a unique API key and then to increase your Kudos. [Read how Kudos are working](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/).


### KoboldAI Client

1. Download the [KoboldAI Client](https://github.com/KoboldAI/KoboldAI-Client) following the instruction it its repository. 
    * If on windows, use the update-koboldai.bat to switch to the UNITED branch
    * If on linux, switch your origin to https://github.com/henk717/koboldai and switch to the united branch
1. Start KoboldAI with play.(bat|sh)
1. In the AI menu on the top, select Online Serves > KoboldAI Horde
1. Type the address you're currently at in the url, and your stored api key. When the menu with the models appear, select all, or a specific model (if you know what you're doing)
1. Enjoy

## REST API

[Full Documentation](/api)

## Services

* [Register New Account](/register)
* [Transfer Kudos](/transfer)

## Community

* Join us [on discord](https://koboldai.org/discord)
* Support the development of the Stable Horde on [Patreon](https://www.patreon.com/db0) or [Github](https://github.com/db0)
* Support the model development  and the main contributor of this horde on [Patreon](https://www.patreon.com/mrseeker)

## Credits

These are the people who made this sotware possible.

* [Db0](https://dbzer0.com) - Development and Maintenance
* VE VORBYDERNE - REST API 
* Ebolam - GUI Integration
* Mr.Seeker - Brainstorming and general model mastery

And of course, everyone contributing their KAI to the horde!
