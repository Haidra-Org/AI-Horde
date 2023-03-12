const does_denoise_strength_affect_steps = false; // This is a server-side bug. Switch to true when fixed.

export default function calculateKudos(width, height, postProcessors, usesControlNet, prompt, hasSourceImage, shareWithLaionEnabled) {
    const result = Math.pow((width * height) - (64*64), 1.75) / Math.pow((1024*1024) - (64*64), 1.75);
    const steps = getAccurateSteps(width, height, postProcessors, prompt);
    let kudos = Math.round((0.1232 * steps) + result * (0.1232 * steps * 8.75), 2);

    for (let i = 0; i < postProcessors.length; i++) {
        kudos = Math.round(kudos * 1.2, 2);
    }

    if (usesControlNet) {
        kudos = Math.round(kudos * 3, 2);
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

    return kudos;
}

function getAccurateSteps(width, height, postProcessors, prompt) {
    // implementation for getAccurateSteps function
}

function countParentheses(prompt) {
    // implementation for countParentheses function
}