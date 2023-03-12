const doesDenoiseStrengthAffectSteps = false; // This is a server-side bug. Switch to true when fixed.

export default function calculateKudos(
    width,
    height,
    steps,
    samplerName,
    hasSourceImage,
    isImg2Img,
    denoisingStrength,
    postProcessors,
    usesControlNet,
    prompt,
    shareWithLaionEnabled
) {
    const result = Math.pow((width * height) - (64 * 64), 1.75) / Math.pow((1024 * 1024) - (64 * 64), 1.75);
    steps = getAccurateSteps(steps, samplerName, hasSourceImage, isImg2Img, denoisingStrength);
    let kudos = Math.round(((0.1232 * steps) + result * (0.1232 * steps * 8.75)) * 100) / 100;

    for (let i = 0; i < postProcessors.length; i++) {
        kudos = Math.round(kudos * 1.2 * 100) / 100;
    }

    if (usesControlNet) {
        kudos = Math.round(kudos * 3 * 100) / 100;
    }

    const weightsCount = countParentheses(prompt);
    kudos += weightsCount;

    if (hasSourceImage) {
        kudos = kudos * 1.5;
    }

    if (postProcessors.includes('RealESRGAN_x4plus')) {
        kudos = kudos * 1.3;
    }
    if (postProcessors.includes('CodeFormers')) {
        kudos = kudos * 1.3;
    }

    let hordeTax = 3;
    if (shareWithLaionEnabled) {
        hordeTax = 1;
    }
    if (kudos < 10) {
        hordeTax -= 1;
    }
    kudos += hordeTax;

    return Math.round(kudos*100)/100;
}

function getAccurateSteps(steps, samplerName, hasSourceImage, isImg2Img, denoisingStrength) {
    if (['k_dpm_adaptive'].includes(samplerName)) {
        return 50;
    }
    if (['k_heun', 'k_dpm_2', 'k_dpm_2_a', 'k_dpmpp_2s_a'].includes(samplerName)) {
        steps *= 2;
    }
    if (hasSourceImage && isImg2Img && doesDenoiseStrengthAffectSteps) {
        steps *= denoisingStrength;
    }
    return steps;
}


function countParentheses(prompt) {
    let openP = false;
    let count = 0;
    for (let i = 0; i < prompt.length; i++) {
        const c = prompt[i];
        if (c === "(") {
            openP = true;
        } else if (c === ")" && openP) {
            openP = false;
            count++;
        }
    }
    return count;
}