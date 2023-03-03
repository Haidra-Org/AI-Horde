from datetime import datetime

class News:

    HORDE_NEWS = [
        {
            "date_published": "2023-03-03",
            "newspiece": "The Horde Ratings are back in action. Go to your typical UI and rate away!",
            "tags": ["ratings"],
            "importance": "Information",
        },
        {
            "date_published": "2023-02-23",
            "newspiece": "KoboldAI Horde has been merged into Stable Horde as a unified AI Horde!",
            "tags": ["text2text", "ai horde"],
            "importance": "Information",
        },
        {
            "date_published": "2023-02-21",
            "newspiece": (
                'The Horde now supports ControlNet on all models! All kudos go to [hlky](https://github.com/hlky) who again weaved the dark magic!'
            ),
            "tags": ["controlnet", "img2img", "hlky"],
            "importance": "Information"
        },
        {
            "date_published": "2023-02-14",
            "newspiece": (
                'You can now use an almost unlimited prompt size thanks to the work of ResidentChief!'
            ),
            "tags": ["text2img", "img2img", "ResidentChief"],
            "importance": "Information"
        },
        {
            "date_published": "2023-02-09",
            "newspiece": (
                'You can now select to generate a higher-sized image using hires_fix, which uses the composition of stable diffusion at 512x512 which tends to be more consistent.'
            ),
            "tags": ["text2img", "img2img", "ResidentChief"],
            "importance": "Information"
        },
        {
            "date_published": "2023-02-03",
            "newspiece": (
                'The horde now supports pix2pix. All you have to do is use img2img as normal and select the pix2pix model!'
            ),
            "tags": ["img2img", "ResidentChief"],
            "importance": "Information"
        },
        {
            "date_published": "2023-01-24",
            "newspiece": (
                'We now support sending tiling requests! Send `"tiling":true` into your payload params to request an image that seamlessly tiles.'
            ),
            "tags": ["text2img", "img2img", "ResidentChief"],
            "importance": "Information"
        },
        {
            "date_published": "2023-01-23",
            "newspiece": (
                "I have tightened the rules around NSFW models. As they seem to be straying into 'unethical' territory even when not explicitly prompted, "
                "I am forced to tighten the safety controls around them. From now on, otherwise generic terms for young people like `girl` ,`boy` etc "
                "Cannot be used on those models. Please either use terms like `woman` or `man` or switch to a non-NSFW model instead."
            ),
            "tags": ["countermeasures", "nsfw"],
            "importance": "Information"
        },
        {
            "date_published": "2023-01-23",
            "newspiece": (
                "The horde now has a [Blender Plugin](https://github.com/benrugg/AI-Render)!"
            ),
            "tags": ["plugin", "blender"],
            "importance": "Information"
        },
        {
            "date_published": "2023-01-18",
            "newspiece": (
                "We now have a [New Discord Bot](https://github.com/ZeldaFan0225/Stable_Horde_Discord), courtesy of Zelda_Fan#0225. Check out [their other bot](https://slashbot.de/) as well! "
                "Only downside is that if you were already logged in to the old bot, you will need to /login again."
            ),
            "importance": "Information"
        },
        {
            "date_published": "2023-01-18",
            "newspiece": (
                "The prompts now support weights! Use them like so `(sub prompt:1.1)` where 1.1 corresponds to +10% weight "
                "You can tweak upwards more like `1.25` or downwards like `0.7`, but don't go above +=30%"
            ),
            "importance": "Information"
        },
        {
            "date_published": "2023-01-12",
            "newspiece": (
                "We plan to be replacing our official discord bot with [new a new codebase](https://github.com/ZeldaFan0225/Stable_Horde_Discord) based on the work of Zelda_Fan#0225. "
                "Once we do, be aware that the controls will be slightly different and you will have to log-in again with your API key."
            ),
            "importance": "Upcoming"
        },
        {
            "date_published": "2023-01-11",
            "newspiece": (
                "The Stable Horde has its first browser extension! "
                "[GenAlt](https://chrome.google.com/webstore/detail/genalt-generated-alt-text/ekbmkapnmnhhgfmjdnchgmcfggibebnn) is an accessibility plugin to help people with bad eyesight always find alt text for images."
                "The extension relies on the Stable Horde's newly added image interrogation capabilities to generate captions which are then serves as the image's alt text."
            ),
            "importance": "Information"
        },
        {
            "date_published": "2023-01-04",
            "newspiece": "We are proud to announce that we have [initiated a collaboration with LAION](https://dbzer0.com/blog/a-collaboration-begins-between-stable-horde-and-laion/) to help them improve their dataset!",
            "importance": "Information"
        },
        {
            "date_published": "2023-01-06",
            "newspiece": (
                "The amount of kudos consumed when generating images [has been slightly adjusted](https://dbzer0.com/blog/sharing-is-caring/). "
                "To simulate the resource costs of the horde, each image generation request will now burn +3 kudos. Those will not go to the generating worker! "
                "However we also have a new opt-in feature: You can choose to share your text2img generations with [LAION](https://laion.ai/). "
                "If you do, this added cost will be just +1 kudos. "
                "We have also updated our Terms of Service to make this more obvious."
            ),
            "importance": "Information"
        },
        {
            "date_published": "2023-01-05",
            "newspiece": "[Worker now have a WebUI](https://dbzer0.com/blog/the-ai-horde-worker-has-a-control-ui/) which they can use to configure themselves. Use it by running `worker-webui.sh/cmd`",
            "importance": "Workers"
        },
        {
            "date_published": "2023-01-04",
            "newspiece": "[You can now interrogate images](https://dbzer0.com/blog/image-interrogations-are-now-available-on-the-stable-horde/) (AKA img2txt) to retrieve information about them such as captions and whether they are NSFW. Check the api/v2/interrogate endpoint documentation.",
            "importance": "Information"
        },
        {
            "date_published": "2023-01-01",
            "newspiece": "Stable Horde can now be used on the automatic1111 Web UI via [an external script](https://github.com/natanjunges/stable-diffusion-webui-stable-horde)",
            "importance": "Information"
        },
        {
            "date_published": "2022-12-30",
            "newspiece": "Stable Horde now supports depth2img! To use it you need to send a source image and select the `Stable Difffusion 2 Depth` model",
            "importance": "Information"
        },
        {
            "date_published": "2022-12-28",
            "newspiece": "Stable Horde workers can now opt-in to loading post-processors. Check your bridge_data.py for options. This should help workers who started being more unstable due to the PP requirements.",
            "importance": "Workers"
        },
        {
            "date_published": "2022-12-24",
            "newspiece": "Stable Horde has now support for [CodeFormer](https://shangchenzhou.com/projects/CodeFormer/). Simply use 'CodeFormers' for your postprocessor (case sensitive). This will fix any faces in the image. Be aware that due to the processing cost of this model, the kudos requirement will be 50% higher!  Note: The inbuilt upscaler has been disabled",
            "importance": "Information"
        },
        {
            "date_published": "2022-12-08",
            "newspiece": "The Stable Horde workers now support dynamically swapping models. This means that models will always switch to support the most in demand models every minute, allowing us to support demand much better!",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-28",
            "newspiece": "The Horde has undertaken a massive code refactoring to allow me to move to a proper SQL DB. This will finally allow me to scale the frontend systems horizontally and allow for way more capacity!",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-24",
            "newspiece": "Due to the massive increase in demand from the Horde, we have to limit the amount of concurrent anonymous requests we can serve. We will revert this once our infrastructure can scale better.",
            "importance": "Crisis"
        },
        {
            "date_published": "2022-11-24",
            "newspiece": "Stable Diffusion 2.0 has been released and now it is available on the Horde as well.",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-22",
            "newspiece": "A new Stable Horde Bot has been deployed, this time for Mastodon. You can find [the stablehorde_generator}(https://sigmoid.social/@stablehorde_generator) as well as our [official Stable Horde account](https://sigmoid.social/@stablehorde) on sigmoid.social",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-22",
            "newspiece": "We now have [support for the Unreal Engine](https://github.com/Mystfit/Unreal-StableDiffusionTools/releases/tag/v0.5.0) via a community-provided plugin",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-18",
            "newspiece": "The stable horde [now supports post-processing](https://www.patreon.com/posts/post-processing-74815675) on images automatically",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-05",
            "newspiece": "Due to suddenly increased demand, we have adjusted how much requests accounts can request before needing to have the kudos upfront. More than 50 steps will require kudos and the max resolution will be adjusted based on the current horde demand.",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-05",
            "newspiece": "Workers can now [join teams](https://www.patreon.com/posts/teams-74247978) to get aggregated stats.",
            "importance": "Information"
        },
        {
            "date_published": "2022-11-02",
            "newspiece": "The horde can now generate images up to 3072x3072 and 500 steps! However you need to already have the kudos to burn to do so!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-29",
            "newspiece": "Inpainting is now available on the stable horde! Many kudos to [blueturtle](https://github.com/blueturtleai) for the support!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-25",
            "newspiece": "Another [Discord Bot for Stable Horde integration](https://github.com/ZeldaFan0225/Stable_Horde_Discord) has appeared!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-24",
            "newspiece": "The Stable Horde Client has been renamed to [Lucid Creations](https://dbzer0.itch.io/lucid-creations) and has a new version and UI out which supports multiple models and img2img!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-22",
            "newspiece": "We have [a new npm SDK](https://github.com/ZeldaFan0225/stable_horde) for integrating into the Stable Horde.",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-22",
            "newspiece": "Krita and GIMP plugins now support img2img",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-21",
            "newspiece": "Image 2 Image is now available for everyone!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-20",
            "newspiece": "Stable Diffusion 1.5 is now available!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-17",
            "newspiece": "We now have [a Krita plugin](https://github.com/blueturtleai/krita-stable-diffusion).",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-17",
            "newspiece": "Img2img on the horde is now on pilot for trusted users.",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-16",
            "newspiece": "Yet [another Web UI](https://tinybots.net/artbot) has appeared.",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-11",
            "newspiece": "A [new dedicated Web UI](https://aqualxx.github.io/stable-ui/) has entered the scene!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-10",
            "newspiece": "You can now contribute a worker to the horde [via google colab](https://colab.research.google.com/github/harrisonvanderbyl/ravenbot-ai/blob/master/Horde.ipynb). Just fill-in your API key and run!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-06",
            "newspiece": "We have a [new installation video](https://youtu.be/wJrp5lpByCc) for both the Stable Horde Client and the Stable horde worker.",
            "importance": "Information"
        },        {
            "date_published": "2023-01-23",
            "newspiece": "All workers must start sending the `bridge_agent` key in their job pop payloads. See API documentation.",
            "importance": "Workers"
        },
        {
            "date_published": "2022-10-10",
            "newspiece": "The [discord rewards bot](https://www.patreon.com/posts/new-kind-of-73097166) has been unleashed. Reward good contributions to the horde directly from the chat!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-13",
            "newspiece": "KoboldAI Has been upgraded to the new countermeasures",
            "tags": ["countermeasures", "ai horde"],
            "importance": "Information",
        },
        {
            "date_published": "2022-10-09",
            "newspiece": "The horde now includes News functionality. Also [In the API!](/api/v2/status/news)",
            "importance": "Information"
        },
    ]

    def get_news(self):
        '''extensible function from gathering nodes from extensing classes'''
        return(self.HORDE_NEWS)

    def sort_news(self, raw_news):
        # unsorted_news = []
        # for piece in raw_news:
        #     piece_dict = {
        #         "date": datetime.strptime(piece["piece"], '%y-%m-%d'),
        #         "piece": piece["news"],
        #     }
        #     unsorted_news.append(piece_dict)
        sorted_news = sorted(raw_news, key=lambda p: datetime.strptime(p["date_published"], '%Y-%m-%d'), reverse=True)
        return(sorted_news)

    def sorted_news(self):
        return(self.sort_news(self.get_news()))