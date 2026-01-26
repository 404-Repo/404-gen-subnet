## Competition 2: "Image to 3D Generation (Mesh)"

### What we provide:
1. A Set of 140k+ 2D Image Prompts for Testing;
2. Our base miner [implementation](https://github.com/404-Repo/404-base-miner-mesh) that is ready for Commercial Use; 
3. Judge Code for Evaluating Mesh Outputs. This can be found in this repository ("judge-service" folder);

### Base miner description:
Our Base Miner Implementation Includes:
1. [Trellis 2 (implementation)](https://github.com/404-Repo/404-base-miner-mesh) (Ready for Commercial Use. Produces Mesh Models Only);
2. Docker File. Location: ["docker"](https://github.com/404-Repo/404-base-miner-mesh/tree/main/docker) folder.

### Base miner: some points for improvement:
1. Current UV-unwrapping can be unreliable and slow;
2. Generated materials for the meshes have artifacts (most likely related to uv unwrapping);
3. Mesh rasterization can be improved or replaced with a better method;

### Requirements for Submitted Solution:
1. **Packages/Libraries/Code**: must be permitted for Commercial Use;
2. **Mesh Generator**: any mesh generator (your solution can be not based on our base miner code) can be used with permissive licenses for commercial use. 
Any submission that will violate this rule will be disqualified.
2. **Correct Mesh File Format**: Models should be stored in <span style="color:green"> GLB </span> file format with an 
approximate size of no more than <span style="color:green"> 200 mb </span>;
3. **Correct Mesh Orientation**: <span style="color:green"> Y-axis up </span>;
4. **Correct Mesh Size**: mesh model must be scaled to fit within a unit cube. If it will be of wrong size, 
your results might not pass Duels evaluation. 
4. **Asset Quality**: the Solution must significantly visually improve the quality of the generated assets compared 
to the output of our base miner;
5. **Proof of work**: We will require Miner to generate Models for 128 prompts per evaluation.
5. **Generation time**: <span style="color:green"> Trimmed Median: ≤ 90 s. Hard Timeout: 180 s (= Failure). </span>;
6. **The Solution** must be published in a <span style="color:green"> public Git repository </span>;


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
