// This is an ES6 module, so your script should be a module too.

// Download kudos_standalone.js module from https://github.com/db0/AI-Horde/blob/main/kudos/kudos_standalone.js
// Save it locally.
// Import it here:
import calculateKudos from './kudos_standalone.js';

// This shows all the parameters that you should provide to the function.
const width = 1024;
const height = 768;
const steps = 50;
const samplerName = 'k_dpm_2';
const hasSourceImage = true;
const isImg2Img = true;
const denoisingStrength = 0.8;
const postProcessors = ['RealESRGAN_x4plus', 'CodeFormers'];
const usesControlNet = false;
const prompt =  '(tag1:1.1) some other info here';
const shareWithLaionEnabled = false;

// Example call to calculation function:
const kudos = calculateKudos(
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
);

console.log(kudos);
