## Competition 0: "Image to 3D Generation (Gaussian Splatting)"

### What we provide:
1. A Set of 140k+ 2D Image Prompts for Testing;
2. Our base miner [implementation](https://github.com/404-Repo/404-base-miner-gs) that is ready for Commercial Use; 
3. Judge Code for Evaluating Gaussian Splat Outputs. This can be found in this repository ("judge-service" folder);

### Base miner description:
Our Base Miner Implementation Includes:
1. [Background removal](https://github.com/404-Repo/404-base-miner/tree/main/background_remover);
2. [Trellis (implementation)](https://github.com/404-Repo/404-base-miner-gs) (Ready for Commercial Use. Produces Gaussian Splatting Models Only);
3. Docker File. Location: ["docker"](https://github.com/404-Repo/404-base-miner-gs/tree/main/docker) folder.

Our Background Remover uses two opensource models for removing the background from the input image:
([BEN 2](https://github.com/PramaLLC/BEN2/) and [BirefNet](https://github.com/ZhengPeng7/BiRefNet)). 
On top of that, we also use a VLM model ([InternVL-3.5-2B](https://huggingface.co/OpenGVLab/InternVL3_5-2B)) to select the better result, 
which is subsequently used by the Trellis model for generating the final Gaussian Splatting Model..

### Requirements for Submitted Solution:
1. **Packages/libraries**: must be permitted for Commercial Use;
2. **Correct GS File Format**: Models should be stored in <span style="color:green"> 3DGS PLY </span> file format with an approximate size of no more than <span style="color:green"> 200 mb </span>;
3. **Correct Orientation**: <span style="color:green"> Y-axis up </span>; for reference: when you upload your generated model to [SuperSplat editor](https://superspl.at/editor). It should not be tilted or upside down;
4. **Asset Quality**: the Solution must significantly visually improve the quality of the generated assets compared to the output of our base miner;
5. **Generation time** must be within <span style="color:green"> 30 seconds </span>;
6. **The Solution** must be published in a <span style="color:green"> public Git repository </span>;

#### Update on Correct Orientation:
In the previous version of the subnet we followed the orientation rules for the generated model proposed by early 3D generative 
AI models. Our previous frame of reference was defined as +Y Axis pointing down. As a result the generated model in standard 
viewers were loaded upside down. This is not compatible with industry standards, therefore, new models 
should be aligned with +Y Axis pointing up.


### Requirements for Docker File:
1. The Repository must include a <span style="color:green"> "docker/Dockerfile" </span>;
2. The Dockerfile must expose port 10006: <span style="color:green"> EXPOSE 10006 </span>;
3. The  Docker Image must run an HTTP Server listening on <span style="color:green"> 0.0.0.0:10006 </span>;
4. Docker Image Build Time: <span style="color:green"> 2-3 Hours Maximum </span>;
5. Docker Container Startup + Warmup: <span style="color:green"> 30 minutes Max </span>;
6. Should be runnable on NVIDIA cards with compute capabilities: <span style="color:green"> 8.9, 9.0, 10.0 and 12.0 </span>;

### Requirements for API / Endpoints:
You need to implement two endpoints:

<span style="color:green"> POST /generate </span>:

- Inputs: 
  - Image File - multipart/form-data; 
  - Seed - fixed generation seed number (for reproducibility);
- Output: 
  - PLY File Stream - application/octet-stream

```python
# FastAPI example:
@app.post("/generate")
async def generate_model(prompt_image_file: UploadFile = File(...), seed: int = Form()) -> Response:
    # Your generation logic here
    return StreamingResponse(ply_stream, media_type="application/octet-stream")
```
<span style="color:green"> GET /health </span>:

- Health Check Endpoint.

```python
# FastAPI example:
@app.get("/health")
def health_check() -> dict[str, str]:
    """ Return if the server is alive """
    return {"status": "healthy"}
```

Your solution:
1. Must handle each request sequentially (one at a time)
2. The Orchestrator will send the next request immediately after receiving response headers
3. The Orchestrator will NOT wait for model download to complete before sending the next request.


### Timing & Request Handling:
Time is measured from Request Sent to Response Headers Received. Body Download Time is NOT counted.
