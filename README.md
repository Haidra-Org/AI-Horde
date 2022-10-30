# AI Horde

A giant crowdsourced distributed cluster for AI Models. This software can support both Image and Text generation, but it needs to run in different mode for each. 
It allows people without a powerful GPU to use Stable Diffusion or Text generation models like GPT/OPT by relying on spare/idle resources provided by the community.
It also allows non-python clients, such as games and apps, to use AI-provided generations.

This software runs in two modes:
   * [Stable Horde](https://stablehorde.net): Stable Diffusion Image Generation
   * [KoboldAI Horde](https://koboldai.net): KoboldAI text generation

For more questions, check the [FAQ](FAQ.md)

# Registering

To use the horde you need to have a registered account, or use anonymous mode.

To register an account, go to the Horde you want to use:
   * [Stable Horde Registration](https://stablehorde.net/register)
   * [KoboldAI Horde Registration](https://koboldai.net/register)

and login with one of the available services. Once you do you'll see a form where you can put a username. Add one in and it will automatically store a user object for you and provide an API key to identify you. **Note that the different hordes do not share databases**, so even if you use the same authentication, you'll get a different user.

Store this API key and use it for your client or bridge.

By logging in first, you can change your username and API key at any time. 
Be aware that the account is unique per authentication service, so even if you use the same email for discord and google, your user ID will be different for each!
We don't store any identifiable information other than the ID string sent by the oauth for your user. We only use this for user uniqueness, and no other purpose.

If you want, you can also create a pseudonymous account, without logging in with oauth. However *we will not maintain such accounts*. If you lose access to it, you'll have to make a new one. If someone copies your API Key, they can impersonate you. You cannot change the username or API key anymore etc. If you don't want these risks, login to one of the available services instead.

If you do not want to login even with a pseudonymous account, you can use this service anonymously by using '0000000000' as your API key. However your usage and contributions will be not be tracked. Be aware that if this service gets too overloaded, anonymous mode might be turned off!

The point of registering is to track your usage and your contributions. The more you contribute to the Horde, the more priority you have. [Read about this here](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/)

# Community

If you have any questions or feedback, we have a vibrant community on [discord](https://discord.gg/3DxrhksKzn)

# Horde-Specific Information

Please see the individual readmes for each specific mode supported by the AI Horde.

   * [Stable Horde Readme](README_StableHorde.md)
   * [KoboldAI Horde Readme](README_KoboldAIHorde.md)
