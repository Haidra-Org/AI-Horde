<!--
SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# {page_title}

<img style="float:right" src="{horde_img_url}" width="300" /> This is an [AI Horde](https://github.com/Haidra-Org/AI-Horde), a free, community-run service for AI image and text generation. Volunteers run [image workers](https://github.com/Haidra-Org/horde-worker-reGen) and [text workers](https://github.com/LostRuins/koboldcpp) on their own GPUs, and anyone can use them. If you like this service, consider [joining the horde yourself](https://github.com/Haidra-Org/AI-Horde#contribute-your-gpu-become-a-worker)!

For more information, see [the FAQ](https://aihorde.net/faq) and [our mission](https://aihorde.net/mission). You can also follow the [main developer's blog](https://dbzer0.com).

## Latest [News](/api/v2/status/news)

{news}

## Sponsors

The AI Horde is paid for by donations. Volunteers donate their GPU time, and financial donations to [Haidra](https://haidra.net), a non-profit, pay for the servers. If the Horde is useful to you, please consider [supporting Haidra](https://haidra.net/donate/).

[![](assets/logo_nlnet.svg)](https://nlnet.nl/project/AI-Horde/)

## Stats

### Image Generation
* Average Recent Performance: {avg_performance} {avg_thing_name} per second.
* Total generated: {total_image_things} {total_total_image_things_name}.
* Total image requests fulfilled: {total_image_fulfillments}{total_image_fulfillments_char}.
* Active [Dreamers](/api/v2/workers?type=image)/Threads: {image_workers}/{image_worker_threads}
* Queue: {total_image_queue} requests for a total of {queued_image_things} {queued_image_things_name}.
### Text Generation
* Average Recent Performance: {avg_text_performance} {avg_text_thing_name} per second.
* Total generated: {total_text_things} {total_text_things_name}.
* Total text requests fulfilled: {total_text_fulfillments}{total_text_fulfillments_char}.
* Active [Scribes](/api/v2/workers?type=text)/Threads: {text_workers}/{text_worker_threads}
* Queue: {total_text_queue} requests for a total of {queued_text_things} {queued_text_things_name}.
### Image Alchemy
* Total processed: {total_forms}{total_forms_char}.
* Active [Alchemists](/api/v2/workers?type=interrogation)/Threads: {interrogation_workers}/{interrogation_worker_threads}
* Queue: {total_forms_queue} alchemy forms.
### Real-time stats
* [Grafana](https://grafana.aihorde.net/) shows current and past horde activity.

## Usage

First [register an account](/register) to get an API key. Store that key somewhere safe.

* If you do not want to register, use `0000000000` as the API key to connect anonymously. Anonymous requests have the lowest priority when the horde is busy.
* To raise your priority, use your own API key and earn kudos. [Read how kudos work](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/kudos.md).

### Apps and integrations

* Generate images in your browser with the [image generator](https://aihorde.net/generate/image). The [image generation guide](https://aihorde.net/docs/image) walks through a first image.
* Write stories or chat in your browser with [KoboldAI Lite](https://lite.koboldai.net).
* The [list of integrations](https://aihorde.net/guis) has many more frontends for images and text, mobile apps, bots, editor plugins, games and developer tools.

## API

* [REST API documentation](/api)
* OpenAI-compatible chat completions at `https://oai.aihorde.net/v1`, with its own [API documentation](https://oai.aihorde.net/docs)
* SDKs for [Python](https://github.com/Haidra-Org/horde-sdk) and [Node.js](https://www.npmjs.com/package/@zeldafan0225/ai_horde)
* [Service documentation](https://haidra-org.github.io/AI-Horde/) with tutorials, how-to guides and reference pages

If you build a paid or ad-supported integration on the AI Horde, we ask that you use part of your profits to support the Horde. If your app relies only on the Horde's volunteer resources, we expect at least 50% of those profits to go to supporting the Horde, preferably by running your own workers. If the Horde is one option among many, we suggest running workers in proportion to how much your users rely on it. [This blog post](https://dbzer0.com/blog/what-about-paid-services-on-top-of-the-ai-horde/) explains the reasoning.

## Services

* [Register New Account](/register)
* [Transfer Kudos](/transfer)

## Community

* Join us on [Discord](https://discord.gg/3DxrhksKzn)
* Follow us on <a rel="me" href="https://sigmoid.social/@stablehorde">Mastodon</a> and [Lemmy](https://lemmy.dbzer0.com/c/aihorde)
* Read the [news](https://aihorde.net/news) and subscribe to the [Division by Zer0 blog](https://dbzer0.com/)
* Support the AI Horde with a [donation to Haidra](https://haidra.net/donate/)

## Credits

These are the people who made this software possible.

* [Db0](https://dbzer0.com) - Development and Maintenance
* [Haidra](https://github.com/haidra-org) - The developer community that maintains and improves the AI Horde ecosystem.
* [Sponsors](/sponsors) - Our complete sponsor list

And of course, everyone contributing their GPU processing to the AI Horde!
