<!--
SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Frequently Asked Questions

## How does this all work?

This is a very big subject which will be later be expanded in a devlog. But putting it simply:

* As a requestor, you send a prompt to the horde which adds it into a smart "first in/first out" queue, prioritizing it according to the amount of Kudos you have. If the request has multiple generations required, it is split into multiple jobs by the horde.
* Any joined workers periodically check-in to the horde and request a job to do. The horde sends them the first available request they can fulfil, by order of kudos-priority, respecting the worker's wishes for specific workers who have priority.
* The worker generates the job's prompt and submits it back to the horde
* Once all the component jobs of a request have been submitted, the horde marks the request as "Done"
* The requestor can at any point check the status of their waiting requests and retrieve the results, even if they're not "done"

## Workers?

![](img_faq/worker.png)

### What is a worker?

A worker is computer with usually a mid-range or higher class GPU, who has installed specific software to generate images via Stable Diffusion or KoboldAI locally and it connected to the horde through what we call the "bridge". It is constantly polling the horde for new generations and receiving Kudos in return for creating them.

### What is in it for the worker?

Generating images for others 24/7 technically costs electricity, we know.

People want to contribute to the horde for many reasons

* They want to provide constant processing power for tools they've built on top of Stable Diffusion or KoboldAI, such as video games, chat bots or software plugins
* They want to accumulate Kudos so that when it's time to generate their own requests they can get them done faster.
* They want to warm their room productively (this is indeed a literal reason people have given)
* They are just nice people and want to support this endeavor.

### Can workers spy on my prompts or generations?

*Technically*, yes. While the worker software and the bridge code is not set to allow it, ultimately the software resides at someone else's computer and is open source. As such, anyone with the know-how can modify their own code to not just see all prompts passing through, but even save all results they generate.

However workers do not have any identifying information about individual requestors as they cannot see their ID or IP.

Nevertheless always request generations as if you're posting in a public forum like using a discord bot. While the horde is technically more private than that, it's a good practice anyway.

### Can I turn off my worker whenever I want?

Yes! We do not require workers to stay always online. We only request that you put your worker in maintenance before you do so to avoid messing with a currently running generation your worker might have picked up.

### Do workers support multiple models

Yes, but you select your model manually. You can also not select a model and get the first model the next worker that picks up your work provides

### Can I prioritize myself and my friends on my own worker?

Yes! By default, your own requests are always served first by your own worker, regardless of Kudos. But you can also specify specific usernames which will also be prioritized the same way.

## Kudos?

![](img_faq/kudos.png)

### What, what is Kudos?

Another big subject. This one actually [has a devlog about it](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/)

### How do I get Kudos?

Connect a worker to the horde, that is all! You will generate kudos for each request you fulfil, relevant to its difficulty, and you will also generate kudos every 10 minutes your worker stays online.

### How is image Kudos consumption calculated?

The Kudos cost reflects the amount of processing required to generate the image.

As each payload on the horde can have too many variables which affect its speed, we have trained a neural network which takes as input a request payload, and calculates how much kudos it would require, based on how much faster or slower it would generate compared to a baseline of 10 kudos for a 50 step 512x512 image. The baseline costs 10 kudos. So  if a payload is expected to take double that time, it will be valued as 20 kudos.

The AI Horde API provides a `dry_run` payload key. When set to true, it will return the kudos cost for an image, without actually requesting a generation.

On top of that there is what's known as the "horde tax" which represents the extra costs to the infrastructure for each request. These kudos are not received by the worker but are rather "burnt" forever.

* There is a 1 kudos tax per request. This is applied even if the request it cancelled, faulted or aborted.
* There is a 1 kudos tax per job in a request.
* When requesting only fast workers, there's an added +20% kudos burn.
* When requesting a worker blacklist, there's an added +10% kudos burn.

### I don't have a powerful GPU. How can I get Kudos?

We use Kudos to support good behaviour in the community. As such we have ways to receive Kudos outside of generating images for others (although that's the best way)

* If you have at least 2GB VRAM, you can run an alchemist, which is used to interrogate or post-process images.
* Rate some images. Almost all clients should have a way to rate images while waiting which will provide kudos per rating, and you can also rate the images you just generated for a kudos refund! [Artbot has a very easy rating page](https://tinybots.net/artbot/rate)
* Fulfill a bounty from our discord bounties forum
* Subscribe to [the patreon supporting the development of the AI Horde](https://www.patreon.com/db0).
* Generate and share some cool art in our discord.
* Politely request some people to transfer some to you in our discord server. People tend to give plenty to new users and helpful or funny comments.

### Can I transfer my Kudos?

**Yes!** Check the `api/v2/transfer` endpoint.

Remember however that the Kudos is merely a prioritization mechanism, **not a currency**. The AI Horde are under no obligation to maintain Kudos totals or current rate of return, and we may tweak them to ensure more optimal operation of the system.

### Can I sell my kudos?

**NO!** Kudos is inherently valueless and we do not allow anyone exchanging kudos for money. Bypassing this requirement is an existential threat to the AI Horde. Please do not attempt to do this under the table. **If you exchange money for Kudos and we discover it, we might zero out your account and whoever you bought it from!**

### Is Kudos a cryptocurrency?

**No!** Kudos is completely centralized and involved no blockchain tech whatsoever. The AI Horde is explicitly hostile to blockchain technologies and we will never integrate with any of them. Likewise, there is no way to convert kudos to anything other than favours benefiting the improvement of the AI Horde.

### Can I exchange kudos for cryptocurrencies?

**No!** Same rules and reasoning applies as for selling kudos, see above.

## Not Safe for Work?

![](img_faq/nsfw.png)

### Can I request NSFW images/text?

Yes, but you might have a smaller a pool of workers to fulfil your request, which will lead to longer generation times.

### Do you censor generations?

Horde-wide, we censor only one type of generation: [CSAM](https://en.wikipedia.org/wiki/Child_sexual_abuse_material) Images. We have two mechanisms to achieve this, one is a regex replacement filter during initial API request. The other is the anti-CSAM AI running on each worker. See more detailed answers below.

Other than this one instance, the horde does not censor text generations at all, or images which do not appear to be CSAM.

However each individual worker might have its own censorship guidelines. And each requestor can voluntarily opt-in to accidental NSFW censorship.

## Why are some of my images just black with white text?

Those generations have been NSFW-censored by the worker generating them. If you've specified your request as SFW, individual SFW workers who fulfil it might have the NSFW censorship model active, which will return just this black image. To avoid such images, turn on NSFW, or ensure your prompt is not too close to the edge of SFW/NSFW. If the image mentions censor due to the anti-CSAM filter, this cannot be turned off.

## Why are some of my images are still censored even though I'm requesting NSFW?

Each individual worker can optionally define a censorlist. If any word inside that list is found, the worker will automatically post-process using a NSFW censorship model. These words are things that should never be combined with NSFW content or would run into legal troubles for the worker if they did.

This means your censored images triggered one such worker's censorlist. You can rerun the prompt and hope you get a generation with a seed that doesn't trigger the NSFW model, or hope to get a new worker, or tweak your prompt.

If you feel a worker is using the censorlist maliciously, or improperly, please contact us with the content of your prompt and the worker name, and we'll address it.

## Can you explain how the Anti-CSAM regex filter works?

When an image request first comes into the AI Horde, it is passed through a private regex filter looking for combination of two contexts: "Underage" context and "Lewd" Context. An example of an underage context might be "child" and an example of a lewd context might be "without clothes".

If none, or one of these contexts is detected in a prompt, then the prompt is allowed to pass through unaffected. For example "child in the playground" is ok. "Without clothes in the bathroom" is also OK.

If however both of these terms are present in the same prompt, this will trigger the regex protection. This has two modes of operation:

If the `replacement_filter` is `true` in your API payload and the prompt is less than 1000 chars, each triggering term will be automatically replaced by an "adult" version of those terms. For example "school" will be replaced by "university". This attempts to point the inference to try and generate safe content.

If however `replacement_filter` is `false` or your prompt is above 1000 chars, then the request is automatically blocked instead, and you get an IP timeout for a couple of minutes. This IP timeout increases further, every time you get caught by the regex filter. This is to prevent people deliberately trying to out the filter to reverse engineer loopholes.

## Can you explain how the Anti-CSAM AI works?

We have written [a detailed devlog about this](https://dbzer0.com/blog/ai-powered-anti-csam-filter-for-stable-diffusion/)

### Where can I read more of NSFW controls?

* [The NSFW Question](https://www.patreon.com/posts/nsfw-question-72771484)
* [Blacklists](https://www.patreon.com/posts/72890784)

## Horde?

![](img_faq/horde.png)

### Why "AI Horde"?

This project started as a way to consolidate resources for the KoboldAI Client. As we needed a name for it, I came up with something thematic for the concept of "Kobolds". "A Horde of Kobolds". When I started doing image generation as well, I kept the "Horde" part.

### Can you explain the terminology around the AI Horde?

We have [a dedicated wiki page](https://github.com/Haidra-Org/AI-Horde/wiki/Terminology) where you can look a lot of the cross-referenced terms commonly mentioned in the context of the AI Horde

### Does the horde spy on my prompts and generations?

No, the horde itself is not storing such details. The prompts and the generations are only stored in-memory transiently and deleted shortly after the generation is delivered or cancelled.

### Why should I use the Horde and not my local PC?

Not everyone has a power GPU in their PC. The horde allows anyone to use fast Stable Diffusion and KoboldAI, not only the ones privileged enough to be able to afford an expensive graphics card.

Furthermore, local clients, even at the best of times, are difficult to setup up and often error prone due to python dependencies. They also need plenty of internet bandwidth to download 4GB of models. The stable horde provides no-install clients, as well as browser clients you can use even on your phone!

Finally if you wanted to provide a service built on image or text generation, you can now use your own PC to power your image generations, and therefore avoid all the complexity and capital costs required with setting up a server infrastructure.

### Why should I use the Horde and not a service like Stability.ai?

Because the Horde is free! You will never need to pay to use the horde. Sure if the demand is high, your delivery speed might not be great, but that is true with other services like midjourney

Second, the Horde gives you all the benefits of a local installation, such as freedom in prompts, while still allowing a browser interface. and flexibility.

Finally unlike many of these services, Horde also provides a fully fledged REST API you can use to integrate your applications, without worrying about costs.

### Why should I use the Horde and not a free service?

Because when the service is free, you're the product!

Other services which run on centralized servers have costs. Someone has to pay for the electricity, and the server infrastructure. The horde is explicit of how these costs are crowdsourced and there's no need for us to ever add anything in the future to change our existing model. Other free services tend to be deliberately obscure in how they use your prompts, results, and data, or explicitly say that your data is going to be the product. Such services eventually pivot their usercount to make money through advertisements and data brokering.

If you're fine with that, go ahead and use them.

Finally a lot of these services do not provide free REST APIs, so if you need to integrate with them, you have to use a browser interface, so that you can see the adverts.


### Can I run my own private horde?

Of course! This software is FOSS and you are welcome to use, modify and share, so long as you respect the AGPL3 license.

If you set up your own horde of course, you will need to also maintain your own workers, as hordes do not share workers with each other.

### Can I built paid services integration into the AI Horde?

Yes, with some restrictions. Due to the voluntary nature of the AI Horde, you **must** give back to the AI horde at least as much as you take out to make a profit. Please see the detailed explanation [in this devlog](https://dbzer0.com/blog/what-about-paid-services-on-top-of-the-ai-horde/)
