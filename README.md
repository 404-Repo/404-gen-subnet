# Subnet Architecture Update Details

We are currently preparing to deploy a **New Incentive Mechanism** that evaluates and rewards 3D solutions rather than 3D outputs (as this subnet has historically done). The reasons for this change are 

1️⃣  Improve Alpha Tokenomics Within Tao-Flow Design.

2️⃣  Significantly Reduce Compute Costs for Miners.

3️⃣  Lower Barrier to Entry for New Participants.

4️⃣  Enable Stronger Short-Term Product & Revenue Focus.

5️⃣  Reward Innovation > Output.

## Transition Period

As of Today we will be Burning 100% of Miner Emissions and are not expecting Miners to submit any results. Organic Traffic remains live and will be served by 404.

## Important Details

### Competitions 🤓 
Miners will compete by providing the best Solution. Our first competition will be Image-to-3D. This simplifies some complexity of the solutions that will need to be submitted (e.g compared to Text-to-3D). This also puts existing Miners in the best position when the competition begins.

### Validation ✅
The winning solution will be evaluated by the current Validation Model - a VLM Judge - again putting existing miners in the best position when the competition begins. We likely will no longer use fast validation since this new setup allows longer validation times.

### Competition Rules 🗒️ 
Exact parameters are currently being tested. It's likely there will be no/limited restrictions on packages for the first competition. We may limit the number of GPUs.
Generation Time Limit will be set as part of the Competition Rules (similar to how in the previous design miners had 30 Second Submission Limit).
Any open-source model will be allowed, but external APIs are not permitted (how we enforce this is still being sorted).

### Reward Formula 🪙 
We will be sharing more details on the new Reward Formula in the coming days.

### Other Considerations
We will need to find a system that prevents overfitting to the competition problem set.  The most likely approach:
>  ~100k image prompts will be selected before the competition and published. Each round, a set of N random prompts will be selected and we will generate N results using each submitted solution. Then we'll have a single or double-elimination tournament using multi-stage duels. 
We are still testing methods for delivering weights of custom and fine-tuned models.

The first competition will likely be setup in a way that not only allows existing miners to leverage their past work and expertise to generate SOTA results but also means that we as subnet can learn how to improve the parameters and framework for subsequent competitions. As ever suggestions are welcome, we will publish updates here regularly and aim to give everyone a timeline for re-launch ASAP.

## Testing your Gaussian Splat Generations
To evaluate the quality of your Gaussian Splat outputs please be aware of the following: 

- Our Judge Code uses **GLM-4.1v-Thinking** to evaluate Miner Duels.
- There are setup scripts and sample code for both rendering and for the judge ([duel-eval](duel-eval)).

> Note: Start the vllm server (`duel-eval/scripts/start_vllm_server.sh`) before running the duels (`duel-eval/scripts/run_duels.py`)