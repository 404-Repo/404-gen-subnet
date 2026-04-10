## Competition 2: Procedural Image-to-3D Generation."
This competition focuses on procedurally generating low-poly 3D models from image prompts. The pipeline must output 
code that defines the model as parameterized primitives with procedural textures. The final model should closely match 
the object in the input image.


### What we provide:
1. 2D Image prompts
2. [rendering-service-js](https://github.com/404-Repo/404-gen-subnet/tree/feat/threejs/render-service-js) to render models and detect potential violations. 
3. Evaluation code ([judge-service](https://github.com/404-Repo/404-gen-subnet/tree/feat/threejs/judge-service)) for judging rendered outputs

### Generator Recommendation:
1. Consider using models that were trained to understand 3D space, multi-view concept. You will need a model 
(VLM or MoE (Mixture of Experts)) with a strong spatial understanding to analyze 2D shape -> decompose it to primitives in 3D.
Your model should also be able to deliver robust THREE.js code.
2. Some interesting benchmarks to consider for selecting the models:
[VSI-Bench](https://vision-x-nyu.github.io/thinking-in-space.github.io/#vsi-leaderboard); 
[SpatialBench](https://spicylemonade.github.io/spatialbench/); 
[MMSI-Bench](https://runsenxu.com/projects/MMSI_Bench/).


### Requirements
Keep the following baseline expectations in mind:

1. Build an agentic system that is capable of generating low-poly meshes from provided image prompts. 
3D model of the object should be defined as a decomposition of primitives with procedurally generated textures. 
The output of the system should be **THREE.js** code that produces textured 3D object from the image.
2. Deliver a runnable, evaluator-compatible miner service in Docker.
3. Use only commercially permitted models/libraries and publish the solution in a public Git repository.
4. Keep builds reproducible by pinning all external dependencies and model revisions.
5. Your solution should be compatible and compliant with provided 
[validation mechanism](https://github.com/404-Repo/404-gen-subnet/tree/feat/threejs/miner-reference/validator) and
[rendering mechanism](https://github.com/404-Repo/404-gen-subnet/tree/feat/threejs/render-service-js).

For exact requirements and acceptance criteria check our provided reference: 
[404 miner reference (threejs branch)](https://github.com/404-Repo/404-gen-subnet/tree/feat/threejs/miner-reference)