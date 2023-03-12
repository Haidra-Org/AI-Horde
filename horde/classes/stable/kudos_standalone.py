class Params:
    def __init__(self, **kwargs):
        self.width = kwargs.get("width")
        self.height = kwargs.get("height")

        self.steps = kwargs.get("steps")

        self.sampler_name = kwargs.get("sampler_name")

        self.has_source_image = kwargs.get("has_source_image")
        self.is_img2img = kwargs.get("is_img2img")
        self.denoising_strength = kwargs.get("denoising_strength")

        self.post_processors = kwargs.get("post_processors")

        self.uses_control_net = kwargs.get("uses_control_net")
        self.prompt = kwargs.get("prompt")

        self.share_with_laion_enabled = kwargs.get("share_with_laion_enabled")


def calculate_kudos(params: Params):
    result = pow((params.width * params.height) - (64*64), 1.75) / pow((1024*1024) - (64*64), 1.75)
    steps = get_accurate_steps(params)
    kudos = round((0.1232 * steps) + result * (0.1232 * steps * 8.75),2)

    for post_processor in range(len(params.post_processors)):
        kudos = round(kudos * 1.2,2)

    if params.uses_control_net:
        kudos = round(kudos * 3,2)

    weights_count = count_parentheses(params.prompt)
    kudos += weights_count

    if params.has_source_image:
        kudos = kudos * 1.5

    if 'RealESRGAN_x4plus' in params.post_processors:
        kudos = kudos * 1.3
    if 'CodeFormers' in params.post_processors:
        kudos = kudos * 1.3

    horde_tax = 3
    if params.share_with_laion_enabled:
        horde_tax = 1
    if kudos < 10:
        horde_tax -= 1
    kudos += horde_tax

    return kudos


def get_accurate_steps(params: Params):
    steps = params.steps
    if params.sampler_name in ['k_dpm_adaptive']:
        return 50
    if params.sampler_name in ['k_heun', "k_dpm_2", "k_dpm_2_a", "k_dpmpp_2s_a"]:
        steps *= 2
    if params.has_source_image and params.is_img2img:
        # 0.8 is the default on nataili
        steps *= params.denoising_strength
    return steps


def count_parentheses(s):
    open_p = False
    count = 0
    for c in s:
        if c == "(":
            open_p = True
        elif c == ")" and open_p:
            open_p = False
            count += 1
    return count

if __name__ == "__main__":
    params = Params(
        width=1024,
        height=768,
        steps=50,
        sampler_name="k_dpm_2",
        has_source_image=True,
        is_img2img=True,
        denoising_strength=0.8,
        post_processors=["RealESRGAN_x4plus", "CodeFormers"],
        uses_control_net=False,
        prompt="(tag1:1.1) some other info here",
        share_with_laion_enabled=False
    )
    print(calculate_kudos(params))