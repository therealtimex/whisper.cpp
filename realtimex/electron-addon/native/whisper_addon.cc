#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <napi.h>
#include <sstream>
#include <string>
#include <vector>

// whisper.cpp
#include "ggml-backend.h" // for ggml_backend_register()
#include "ggml-cpu.h"     // for ggml_backend_cpu_reg()
#include "whisper.h"

// Audio pipeline
#include "audio_pipeline.h"

namespace {
struct EngineState {
  bool initialized = false;
  std::string model_path;
  uint32_t threads = 1;
  std::string language;
  bool detect_language = true;
} state;

struct WhisperContextDeleter {
  void operator()(whisper_context *ctx) const {
    if (ctx) {
      whisper_free(ctx);
    }
  }
};

struct WhisperVADDeleter {
  void operator()(whisper_vad_context *ctx) const {
    if (ctx) {
      whisper_vad_free(ctx);
    }
  }
};

// Per-session context structure
struct WhisperSessionContext {
  std::string session_id;
  std::unique_ptr<whisper_context, WhisperContextDeleter> ctx;
  std::unique_ptr<whisper_vad_context, WhisperVADDeleter> vad_ctx;
  std::unique_ptr<audio::AudioPipeline> pipeline;
  std::mutex mutex; // Per-session mutex for parallel transcription
  uint32_t threads;
  std::string language;
  bool detect_language;

  WhisperSessionContext(const std::string& id)
    : session_id(id), threads(1), detect_language(true) {}
};

// Global session map (protected by its own mutex)
static std::map<std::string, std::shared_ptr<WhisperSessionContext>> g_sessions;
static std::mutex g_sessions_mutex;

// Legacy global context for backward compatibility (deprecated)
static std::unique_ptr<whisper_context, WhisperContextDeleter> g_ctx;
static std::unique_ptr<whisper_vad_context, WhisperVADDeleter> g_vad_ctx;
static std::mutex g_mutex;
static std::unique_ptr<audio::AudioPipeline> g_pipeline;

inline bool IsTruthy(const Napi::Value &value) {
  if (value.IsBoolean())
    return value.As<Napi::Boolean>().Value();
  if (value.IsNumber())
    return value.As<Napi::Number>().DoubleValue() != 0.0;
  if (value.IsString()) {
    const std::string s = value.As<Napi::String>().Utf8Value();
    return !s.empty() && s != "0" && s != "false";
  }
  return false;
}

inline float ClampSample(float v) {
  if (v > 1.0f)
    return 1.0f;
  if (v < -1.0f)
    return -1.0f;
  return v;
}

std::vector<float> MixToMono(const float *data, size_t frames,
                             uint32_t channels) {
  if (channels <= 1) {
    return std::vector<float>(data, data + frames);
  }

  std::vector<float> mono(frames, 0.0f);
  for (size_t i = 0; i < frames; ++i) {
    float sum = 0.0f;
    const size_t base = i * channels;
    for (uint32_t c = 0; c < channels; ++c) {
      sum += data[base + c];
    }
    mono[i] = sum / static_cast<float>(channels);
  }
  return mono;
}

std::vector<float> ResampleLinear(const std::vector<float> &input,
                                  uint32_t in_rate, uint32_t out_rate) {
  if (input.empty() || in_rate == 0 || out_rate == 0 || in_rate == out_rate) {
    return input;
  }

  const double ratio =
      static_cast<double>(in_rate) / static_cast<double>(out_rate);
  const size_t out_frames = static_cast<size_t>(
      std::floor(static_cast<double>(input.size()) / ratio));
  if (out_frames == 0) {
    return {};
  }

  std::vector<float> output(out_frames, 0.0f);
  for (size_t i = 0; i < out_frames; ++i) {
    const double pos = static_cast<double>(i) * ratio;
    const size_t base = static_cast<size_t>(pos);
    const size_t next = std::min(base + 1, input.size() - 1);
    const double frac = pos - static_cast<double>(base);
    output[i] = ClampSample(
        static_cast<float>(input[base] * (1.0 - frac) + input[next] * frac));
  }

  return output;
}

Napi::Value Initialize(const Napi::CallbackInfo &info) {
  const Napi::Env env = info.Env();

  // Pipeline configuration with defaults
  audio::PipelineConfig pipelineConfig;
  std::string vad_model_path; // VAD model path (parsed from options)

  if (info.Length() > 0 && info[0].IsObject()) {
    const Napi::Object opts = info[0].As<Napi::Object>();
    if (opts.Has("modelPath")) {
      state.model_path = opts.Get("modelPath").ToString();
    }
    if (opts.Has("threads") && opts.Get("threads").IsNumber()) {
      const int32_t t = opts.Get("threads").As<Napi::Number>().Int32Value();
      state.threads = t > 0 ? static_cast<uint32_t>(t) : 1;
    }
    if (opts.Has("language") && opts.Get("language").IsString()) {
      state.language = opts.Get("language").ToString().Utf8Value();
      state.detect_language = state.language.empty();
    }

    // Parse pipeline options
    if (opts.Has("inputSampleRate") && opts.Get("inputSampleRate").IsNumber()) {
      pipelineConfig.inputSampleRate =
          opts.Get("inputSampleRate").As<Napi::Number>().Uint32Value();
    }
    if (opts.Has("outputSampleRate") &&
        opts.Get("outputSampleRate").IsNumber()) {
      pipelineConfig.outputSampleRate =
          opts.Get("outputSampleRate").As<Napi::Number>().Uint32Value();
    }
    if (opts.Has("inputChannels") && opts.Get("inputChannels").IsNumber()) {
      pipelineConfig.inputChannels =
          opts.Get("inputChannels").As<Napi::Number>().Uint32Value();
    }
    if (opts.Has("vadEnergyThreshold") &&
        opts.Get("vadEnergyThreshold").IsNumber()) {
      pipelineConfig.vadEnergyThreshold =
          opts.Get("vadEnergyThreshold").As<Napi::Number>().FloatValue();
    }
    if (opts.Has("vadAttackMs") && opts.Get("vadAttackMs").IsNumber()) {
      pipelineConfig.vadAttackMs =
          opts.Get("vadAttackMs").As<Napi::Number>().Uint32Value();
    }
    if (opts.Has("vadReleaseMs") && opts.Get("vadReleaseMs").IsNumber()) {
      pipelineConfig.vadReleaseMs =
          opts.Get("vadReleaseMs").As<Napi::Number>().Uint32Value();
    }
    if (opts.Has("minSpeechMs") && opts.Get("minSpeechMs").IsNumber()) {
      pipelineConfig.minSpeechMs =
          opts.Get("minSpeechMs").As<Napi::Number>().Uint32Value();
    }
    if (opts.Has("minSegmentMs") && opts.Get("minSegmentMs").IsNumber()) {
      pipelineConfig.minSegmentMs =
          opts.Get("minSegmentMs").As<Napi::Number>().Uint32Value();
    }
    if (opts.Has("maxSegmentMs") && opts.Get("maxSegmentMs").IsNumber()) {
      pipelineConfig.maxSegmentMs =
          opts.Get("maxSegmentMs").As<Napi::Number>().Uint32Value();
    }
    if (opts.Has("preSpeechPadMs") && opts.Get("preSpeechPadMs").IsNumber()) {
      pipelineConfig.preSpeechPadMs =
          opts.Get("preSpeechPadMs").As<Napi::Number>().Uint32Value();
    }
    if (opts.Has("postSpeechPadMs") && opts.Get("postSpeechPadMs").IsNumber()) {
      pipelineConfig.postSpeechPadMs =
          opts.Get("postSpeechPadMs").As<Napi::Number>().Uint32Value();
    }

    // Parse VAD model path
    if (opts.Has("vadModelPath") && opts.Get("vadModelPath").IsString()) {
      vad_model_path = opts.Get("vadModelPath").ToString().Utf8Value();
    }
  }

  if (state.model_path.empty()) {
    Napi::Error::New(env, "modelPath is required").ThrowAsJavaScriptException();
    return env.Null();
  }

  {
    std::lock_guard<std::mutex> lock(g_mutex);
    std::ifstream infile(state.model_path, std::ios::binary);
    if (!infile.good()) {
      Napi::Error::New(env, "Model file not found: " + state.model_path)
          .ThrowAsJavaScriptException();
      return env.Null();
    }

    whisper_log_set(
        [](enum ggml_log_level level, const char *text, void *) {
          if (!text)
            return;
          // keep logs minimal; stderr is noisy otherwise
          if (level == GGML_LOG_LEVEL_WARN || level == GGML_LOG_LEVEL_ERROR) {
            // fprintf(stderr, "[whisper] %s", text);
          }
        },
        nullptr);

    struct whisper_context_params cparams = whisper_context_default_params();
    // Enable GPU acceleration (Metal on macOS, CUDA on Linux/Windows)
    cparams.use_gpu = true;
    cparams.flash_attn = false;
    cparams.gpu_device = 0;

    g_ctx.reset();
    whisper_context *ctx =
        whisper_init_from_file_with_params(state.model_path.c_str(), cparams);
    if (!ctx) {
      Napi::Error::New(env, "Failed to load whisper model")
          .ThrowAsJavaScriptException();
      return env.Null();
    }
    g_ctx.reset(ctx);

    // Initialize Whisper VAD (Silero model)
    // If no VAD model path provided, use default (same directory as Whisper
    // model)
    if (vad_model_path.empty()) {
      const size_t last_slash = state.model_path.find_last_of("/\\");
      if (last_slash != std::string::npos) {
        vad_model_path = state.model_path.substr(0, last_slash + 1) +
                         "ggml-silero-v6.2.0.bin";
      }
    }

    whisper_vad_context *vad_ctx = nullptr;
    if (!vad_model_path.empty()) {
      std::ifstream vad_check(vad_model_path, std::ios::binary);
      if (vad_check.good()) {
        whisper_vad_context_params vad_cparams =
            whisper_vad_default_context_params();
        vad_cparams.n_threads = static_cast<int>(state.threads);

        vad_ctx = whisper_vad_init_from_file_with_params(vad_model_path.c_str(),
                                                         vad_cparams);

        if (vad_ctx) {
          g_vad_ctx.reset(vad_ctx);
        }
      }
    }

    // Initialize audio pipeline
    g_pipeline = std::make_unique<audio::AudioPipeline>(pipelineConfig);

    // Set VAD context if available
    if (vad_ctx) {
      g_pipeline->SetVADContext(vad_ctx);
    }
  }

  state.initialized = true;

  Napi::Object result = Napi::Object::New(env);
  result.Set("ok", true);
  result.Set("modelPath", state.model_path);
  result.Set("threads", state.threads);
  return result;
}

Napi::Value Dispose(const Napi::CallbackInfo &info) {
  (void)info;

  // Acquire mutex before disposing to prevent race with active async operations
  std::lock_guard<std::mutex> lock(g_mutex);

  state = EngineState{};
  g_pipeline.reset();
  g_vad_ctx.reset();

  return info.Env().Undefined();
}

// NEW: Initialize session-specific context for parallel transcription
Napi::Value InitializeSession(const Napi::CallbackInfo &info) {
  Napi::Env env = info.Env();

  if (info.Length() < 1 || !info[0].IsObject()) {
    Napi::TypeError::New(env, "Options object required").ThrowAsJavaScriptException();
    return env.Null();
  }

  Napi::Object opts = info[0].As<Napi::Object>();

  // Session ID is required
  if (!opts.Has("sessionId") || !opts.Get("sessionId").IsString()) {
    Napi::TypeError::New(env, "sessionId is required").ThrowAsJavaScriptException();
    return env.Null();
  }

  std::string session_id = opts.Get("sessionId").ToString().Utf8Value();
  std::string model_path;
  std::string vad_model_path;
  audio::PipelineConfig pipelineConfig;

  // Parse options (same as Initialize)
  if (opts.Has("modelPath") && opts.Get("modelPath").IsString()) {
    model_path = opts.Get("modelPath").ToString().Utf8Value();
  }
  if (model_path.empty()) {
    Napi::Error::New(env, "modelPath is required").ThrowAsJavaScriptException();
    return env.Null();
  }

  // Create session context
  auto session_ctx = std::make_shared<WhisperSessionContext>(session_id);

  // Parse options
  if (opts.Has("threads") && opts.Get("threads").IsNumber()) {
    const int32_t t = opts.Get("threads").As<Napi::Number>().Int32Value();
    session_ctx->threads = t > 0 ? static_cast<uint32_t>(t) : 1;
  }
  if (opts.Has("language") && opts.Get("language").IsString()) {
    session_ctx->language = opts.Get("language").ToString().Utf8Value();
    session_ctx->detect_language = session_ctx->language.empty();
  }

  // Parse pipeline options
  if (opts.Has("inputSampleRate") && opts.Get("inputSampleRate").IsNumber()) {
    pipelineConfig.inputSampleRate = opts.Get("inputSampleRate").As<Napi::Number>().Uint32Value();
  }
  if (opts.Has("outputSampleRate") && opts.Get("outputSampleRate").IsNumber()) {
    pipelineConfig.outputSampleRate = opts.Get("outputSampleRate").As<Napi::Number>().Uint32Value();
  }
  if (opts.Has("inputChannels") && opts.Get("inputChannels").IsNumber()) {
    pipelineConfig.inputChannels = opts.Get("inputChannels").As<Napi::Number>().Uint32Value();
  }
  if (opts.Has("vadModelPath") && opts.Get("vadModelPath").IsString()) {
    vad_model_path = opts.Get("vadModelPath").ToString().Utf8Value();
  }

  // Load Whisper model
  std::ifstream infile(model_path, std::ios::binary);
  if (!infile.good()) {
    Napi::Error::New(env, "Model file not found: " + model_path).ThrowAsJavaScriptException();
    return env.Null();
  }

  struct whisper_context_params cparams = whisper_context_default_params();
  cparams.use_gpu = true;
  cparams.flash_attn = false;
  cparams.gpu_device = 0;

  whisper_context *ctx = whisper_init_from_file_with_params(model_path.c_str(), cparams);
  if (!ctx) {
    Napi::Error::New(env, "Failed to load whisper model for session " + session_id).ThrowAsJavaScriptException();
    return env.Null();
  }
  session_ctx->ctx.reset(ctx);

  // Load VAD model if available
  if (vad_model_path.empty()) {
    const size_t last_slash = model_path.find_last_of("/\\");
    if (last_slash != std::string::npos) {
      vad_model_path = model_path.substr(0, last_slash + 1) + "ggml-silero-v6.2.0.bin";
    }
  }

  whisper_vad_context *vad_ctx = nullptr;
  if (!vad_model_path.empty()) {
    std::ifstream vad_check(vad_model_path, std::ios::binary);
    if (vad_check.good()) {
      whisper_vad_context_params vad_cparams = whisper_vad_default_context_params();
      vad_cparams.n_threads = static_cast<int>(session_ctx->threads);
      vad_ctx = whisper_vad_init_from_file_with_params(vad_model_path.c_str(), vad_cparams);
      if (vad_ctx) {
        session_ctx->vad_ctx.reset(vad_ctx);
      }
    }
  }

  // Initialize audio pipeline
  session_ctx->pipeline = std::make_unique<audio::AudioPipeline>(pipelineConfig);
  if (vad_ctx) {
    session_ctx->pipeline->SetVADContext(vad_ctx);
  }

  // Store session in map
  {
    std::lock_guard<std::mutex> lock(g_sessions_mutex);
    g_sessions[session_id] = session_ctx;
  }

  Napi::Object result = Napi::Object::New(env);
  result.Set("ok", true);
  result.Set("sessionId", session_id);
  result.Set("threads", session_ctx->threads);
  return result;
}

// NEW: Destroy session-specific context
Napi::Value DestroySession(const Napi::CallbackInfo &info) {
  Napi::Env env = info.Env();

  if (info.Length() < 1 || !info[0].IsString()) {
    Napi::TypeError::New(env, "sessionId string required").ThrowAsJavaScriptException();
    return env.Null();
  }

  std::string session_id = info[0].As<Napi::String>().Utf8Value();

  {
    std::lock_guard<std::mutex> lock(g_sessions_mutex);
    auto it = g_sessions.find(session_id);
    if (it != g_sessions.end()) {
      // Session mutex will be locked if transcription is in progress
      // This will block until transcription completes
      std::lock_guard<std::mutex> session_lock(it->second->mutex);
      g_sessions.erase(it);
    }
  }

  Napi::Object result = Napi::Object::New(env);
  result.Set("ok", true);
  return result;
}

// Helper: Get session context
std::shared_ptr<WhisperSessionContext> GetSession(const std::string& session_id) {
  std::lock_guard<std::mutex> lock(g_sessions_mutex);
  auto it = g_sessions.find(session_id);
  if (it != g_sessions.end()) {
    return it->second;
  }
  return nullptr;
}

bool FillInput(const Napi::Env &env, const Napi::Value &value,
               Napi::Float32Array *out_array, uint32_t *sample_rate,
               uint32_t *channels) {
  (void)env;
  if (!value.IsTypedArray()) {
    return false;
  }

  *out_array = value.As<Napi::Float32Array>();
  *sample_rate = 16000;
  *channels = 1;
  return true;
}

bool ParseOptions(const Napi::Env &env, const Napi::Value &value,
                  uint32_t *sample_rate, uint32_t *channels,
                  std::string *language) {
  (void)env;
  if (!value.IsObject()) {
    return false;
  }
  const Napi::Object opts = value.As<Napi::Object>();
  if (opts.Has("sampleRate") && opts.Get("sampleRate").IsNumber()) {
    *sample_rate = opts.Get("sampleRate").As<Napi::Number>().Uint32Value();
  }
  if (opts.Has("channels") && opts.Get("channels").IsNumber()) {
    *channels = opts.Get("channels").As<Napi::Number>().Uint32Value();
  }
  if (language && opts.Has("language") && opts.Get("language").IsString()) {
    *language = opts.Get("language").As<Napi::String>().Utf8Value();
  }
  return true;
}

// AsyncWorker for non-blocking transcription
class TranscribeAsyncWorker : public Napi::AsyncWorker {
public:
  TranscribeAsyncWorker(Napi::Function &callback,
                        std::vector<float> &&audio_data, std::string language,
                        size_t threads, std::string session_id = "")
      : Napi::AsyncWorker(callback), audio_data_(std::move(audio_data)),
        language_(std::move(language)), threads_(threads),
        session_id_(std::move(session_id)) {}

protected:
  void Execute() override {
    // Calculate RMS in Execute to verify audio data
    double rms = 0.0;
    for (const float &s : audio_data_) {
      rms += s * s;
    }
    rms = std::sqrt(rms / audio_data_.size());

    // Try session-based context first (NEW), fallback to global context (LEGACY)
    std::shared_ptr<WhisperSessionContext> session_ctx;
    whisper_context *ctx_ptr = nullptr;
    std::unique_lock<std::mutex> lock;

    if (!session_id_.empty()) {
      // NEW: Session-based context for parallel transcription
      session_ctx = GetSession(session_id_);
      if (!session_ctx || !session_ctx->ctx) {
        SetError("whisper session context not found: " + session_id_);
        return;
      }
      // Lock only THIS session's mutex (allows other sessions to transcribe in parallel)
      lock = std::unique_lock<std::mutex>(session_ctx->mutex);
      ctx_ptr = session_ctx->ctx.get();
    } else {
      // LEGACY: Global context (serialized)
      lock = std::unique_lock<std::mutex>(g_mutex);
      if (!g_ctx) {
        SetError("whisper context not loaded");
        return;
      }
      ctx_ptr = g_ctx.get();
    }

    struct whisper_full_params params =
        whisper_full_default_params(WHISPER_SAMPLING_GREEDY);

    // CPU Optimization: Use configured thread count from JavaScript
    // User can configure 1-4 threads via UI settings (Performance tab)
    // Lower threads = lower CPU usage but slower transcription
    const int max_threads = 4; // Maximum allowed threads
    params.n_threads = std::min(static_cast<int>(threads_), max_threads);

    // CRITICAL FIX: Real-time optimized parameters (like Meetily)
    // Default beam_size=5 causes 5x CPU usage! Use beam_size=1 for real-time.
    params.strategy = WHISPER_SAMPLING_GREEDY;
    params.greedy.best_of = 1;  // Single best result (instead of default 5)
    // Note: beam_size only applies to BEAM_SEARCH strategy, not GREEDY

    params.translate = false;
    params.no_context = false; // Enable context for better accuracy
    params.single_segment = false;

    // Real-time speed optimizations
    params.audio_ctx = 0;        // Use all audio context
    params.max_len = 0;          // No max length limit (process all audio)

    // Speech detection parameters
    params.thold_pt = 0.01f;     // Lower probability threshold for better detection
    params.thold_ptsum = 0.01f;  // Lower sum threshold

    if (!language_.empty()) {
      params.language = language_.c_str();
      params.detect_language = false;
    } else {
      params.detect_language = true;
    }

    // Log thread count for debugging (remove in production)
    // fprintf(stderr, "[Whisper] Transcribing with %d threads (max: 2)\n", params.n_threads);

    auto start = std::chrono::high_resolution_clock::now();
    const int ret = whisper_full(ctx_ptr, params, audio_data_.data(),
                                 static_cast<int>(audio_data_.size()));
    auto end = std::chrono::high_resolution_clock::now();
    auto duration =
        std::chrono::duration_cast<std::chrono::milliseconds>(end - start)
            .count();

    if (ret != 0) {
      SetError("whisper_full failed");
      return;
    }

    const int n_segments = whisper_full_n_segments(ctx_ptr);

    std::ostringstream oss;
    for (int i = 0; i < n_segments; ++i) {
      const char *seg = whisper_full_get_segment_text(ctx_ptr, i);
      if (seg) {
        oss << seg;
      }
    }
    result_text_ = oss.str();
  }

  void OnOK() override {
    Napi::HandleScope scope(Env());
    Napi::Object result = Napi::Object::New(Env());
    result.Set("text", result_text_);
    result.Set("confidence", 0.9);
    result.Set("frames", static_cast<uint32_t>(audio_data_.size()));

    Callback().Call({Env().Null(), result});
  }

  void OnError(const Napi::Error &e) override {
    Napi::HandleScope scope(Env());
    Callback().Call({e.Value(), Env().Null()});
  }

private:
  std::vector<float> audio_data_;
  std::string language_;
  size_t threads_;
  std::string session_id_; // NEW: Session ID for per-context transcription
  std::string result_text_;
};

Napi::Value TranscribeFloat32(const Napi::CallbackInfo &info) {
  const Napi::Env env = info.Env();
  if (!state.initialized) {
    Napi::Error::New(env, "whisper_addon not initialized")
        .ThrowAsJavaScriptException();
    return env.Null();
  }

  if (info.Length() < 1) {
    Napi::TypeError::New(env, "Expected samples Float32Array")
        .ThrowAsJavaScriptException();
    return env.Null();
  }

  Napi::Float32Array input;
  uint32_t sample_rate = 16000;
  uint32_t channels = 1;
  if (!FillInput(env, info[0], &input, &sample_rate, &channels)) {
    Napi::TypeError::New(env, "First argument must be Float32Array")
        .ThrowAsJavaScriptException();
    return env.Null();
  }

  if (info.Length() > 1) {
    ParseOptions(env, info[1], &sample_rate, &channels, nullptr);
  }

  const size_t frames = input.ElementLength() / std::max<uint32_t>(1, channels);
  const std::vector<float> mono = MixToMono(input.Data(), frames, channels);
  const std::vector<float> resampled = ResampleLinear(mono, sample_rate, 16000);

  std::string text;
  {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_ctx) {
      Napi::Error::New(env, "whisper context not loaded")
          .ThrowAsJavaScriptException();
      return env.Null();
    }

    struct whisper_full_params params =
        whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    params.n_threads = static_cast<int>(state.threads);

    // CRITICAL FIX: Real-time optimized parameters (like Meetily)
    params.strategy = WHISPER_SAMPLING_GREEDY;
    params.greedy.best_of = 1;  // Single best result (instead of default 5)

    params.translate = false;
    params.no_context = true;
    params.single_segment = false;

    if (!state.language.empty()) {
      params.language = state.language.c_str();
      params.detect_language = false;
    } else {
      params.detect_language = true;
    }

    const int ret = whisper_full(g_ctx.get(), params, resampled.data(),
                                 static_cast<int>(resampled.size()));
    if (ret != 0) {
      Napi::Error::New(env, "whisper_full failed").ThrowAsJavaScriptException();
      return env.Null();
    }

    const int n_segments = whisper_full_n_segments(g_ctx.get());
    std::ostringstream oss;
    for (int i = 0; i < n_segments; ++i) {
      const char *seg = whisper_full_get_segment_text(g_ctx.get(), i);
      if (seg) {
        oss << seg;
      }
    }
    text = oss.str();
  }

  Napi::Object result = Napi::Object::New(env);
  result.Set("text", text);
  result.Set("confidence", 0.9);
  result.Set("frames", static_cast<uint32_t>(resampled.size()));
  return result;
}

Napi::Value ProcessAudio(const Napi::CallbackInfo &info) {
  const Napi::Env env = info.Env();
  if (info.Length() < 1 || !info[0].IsObject()) {
    Napi::TypeError::New(env, "Expected options object")
        .ThrowAsJavaScriptException();
    return env.Null();
  }

  const Napi::Object payload = info[0].As<Napi::Object>();
  if (!payload.Has("samples")) {
    Napi::TypeError::New(env, "Missing samples field")
        .ThrowAsJavaScriptException();
    return env.Null();
  }

  const Napi::Value samples_val = payload.Get("samples");
  if (!samples_val.IsTypedArray()) {
    Napi::TypeError::New(env, "samples must be Float32Array")
        .ThrowAsJavaScriptException();
    return env.Null();
  }

  Napi::Float32Array input = samples_val.As<Napi::Float32Array>();
  uint32_t channels =
      payload.Has("channels") && payload.Get("channels").IsNumber()
          ? payload.Get("channels").As<Napi::Number>().Uint32Value()
          : 2;

  // NEW: Extract sessionId if provided
  std::string session_id;
  if (payload.Has("sessionId") && payload.Get("sessionId").IsString()) {
    session_id = payload.Get("sessionId").ToString().Utf8Value();
  }

  if (session_id.empty()) {
    if (!state.initialized) {
      Napi::Error::New(env, "whisper_addon not initialized")
          .ThrowAsJavaScriptException();
      return env.Null();
    }

    if (!g_pipeline) {
      Napi::Error::New(env, "audio pipeline not initialized")
          .ThrowAsJavaScriptException();
      return env.Null();
    }
  }

  const size_t frames = input.ElementLength() / std::max<uint32_t>(1, channels);

  // Process through the pipeline with mutex protection
  // This prevents race condition if Reset/Flush is called simultaneously
  std::vector<audio::AudioSegment> segments;

  if (!session_id.empty()) {
    // NEW: Use session-specific pipeline
    auto session_ctx = GetSession(session_id);
    if (!session_ctx || !session_ctx->pipeline) {
      Napi::Error::New(env, "Session pipeline not found: " + session_id).ThrowAsJavaScriptException();
      return env.Null();
    }
    std::lock_guard<std::mutex> lock(session_ctx->mutex);
    segments = session_ctx->pipeline->Process(input.Data(), frames);
  } else {
    // LEGACY: Use global pipeline
    std::lock_guard<std::mutex> lock(g_mutex);
    segments = g_pipeline->Process(input.Data(), frames);
  }

  // Convert segments to JavaScript array
  Napi::Array jsSegments = Napi::Array::New(env, segments.size());
  for (size_t i = 0; i < segments.size(); ++i) {
    const auto &seg = segments[i];

    // Copy segment samples to JavaScript buffer
    const size_t byte_length = seg.samples.size() * sizeof(float);
    Napi::ArrayBuffer buffer = Napi::ArrayBuffer::New(env, byte_length);
    std::memcpy(buffer.Data(), seg.samples.data(), byte_length);
    Napi::Float32Array samplesArray =
        Napi::Float32Array::New(env, seg.samples.size(), buffer, 0);

    // Create segment object
    Napi::Object jsSegment = Napi::Object::New(env);
    jsSegment.Set("samples", samplesArray);
    jsSegment.Set("durationMs", seg.durationMs);
    jsSegment.Set("sampleRate", 16000);
    jsSegment.Set("channels", 1);

    jsSegments[i] = jsSegment;
  }

  // Return result object with segments
  Napi::Object result = Napi::Object::New(env);
  result.Set("segments", jsSegments);
  return result;
}

// Flush pending audio segments from the pipeline
Napi::Value FlushAudio(const Napi::CallbackInfo &info) {
  const Napi::Env env = info.Env();

  // NEW: Extract sessionId if provided
  std::string session_id;
  if (info.Length() > 0 && info[0].IsString()) {
    session_id = info[0].As<Napi::String>().Utf8Value();
  }

  std::vector<audio::AudioSegment> segments;

  if (!session_id.empty()) {
    // NEW: Use session-specific pipeline
    auto session_ctx = GetSession(session_id);
    if (!session_ctx || !session_ctx->pipeline) {
      // Return empty result if session not found
      Napi::Object result = Napi::Object::New(env);
      result.Set("segments", Napi::Array::New(env, 0));
      return result;
    }
    std::lock_guard<std::mutex> lock(session_ctx->mutex);
    segments = session_ctx->pipeline->Flush();
  } else {
    // LEGACY: Use global pipeline
    if (!state.initialized || !g_pipeline) {
      // Return empty result if not initialized
      Napi::Object result = Napi::Object::New(env);
      result.Set("segments", Napi::Array::New(env, 0));
      return result;
    }
    std::lock_guard<std::mutex> lock(g_mutex);
    segments = g_pipeline->Flush();
  }

  // Convert segments to JavaScript array
  Napi::Array jsSegments = Napi::Array::New(env, segments.size());
  for (size_t i = 0; i < segments.size(); ++i) {
    const auto &seg = segments[i];

    // Copy segment samples to JavaScript buffer
    const size_t byte_length = seg.samples.size() * sizeof(float);
    Napi::ArrayBuffer buffer = Napi::ArrayBuffer::New(env, byte_length);
    std::memcpy(buffer.Data(), seg.samples.data(), byte_length);
    Napi::Float32Array samplesArray =
        Napi::Float32Array::New(env, seg.samples.size(), buffer, 0);

    // Create segment object
    Napi::Object jsSegment = Napi::Object::New(env);
    jsSegment.Set("samples", samplesArray);
    jsSegment.Set("durationMs", seg.durationMs);
    jsSegment.Set("sampleRate", 16000);
    jsSegment.Set("channels", 1);

    jsSegments[i] = jsSegment;
  }

  // Return result object with flushed segments
  Napi::Object result = Napi::Object::New(env);
  result.Set("segments", jsSegments);
  return result;
}

// Reset the audio pipeline state between recording sessions
Napi::Value ResetPipeline(const Napi::CallbackInfo &info) {
  const Napi::Env env = info.Env();

  // NEW: Extract sessionId if provided
  std::string session_id;
  if (info.Length() > 0 && info[0].IsString()) {
    session_id = info[0].As<Napi::String>().Utf8Value();
  }

  if (!session_id.empty()) {
    // NEW: Reset session-specific pipeline
    auto session_ctx = GetSession(session_id);
    if (!session_ctx || !session_ctx->pipeline) {
      return env.Undefined();
    }
    // Acquire mutex before resetting to prevent race condition
    std::lock_guard<std::mutex> lock(session_ctx->mutex);
    session_ctx->pipeline->Reset();
  } else {
    // LEGACY: Reset global pipeline
    if (!g_pipeline) {
      return env.Undefined();
    }
    // Acquire mutex before resetting to prevent race condition with async transcription
    // Without this lock, Reset() can invalidate buffers while worker thread is accessing them,
    // causing data race and freeze (especially on Windows)
    std::lock_guard<std::mutex> lock(g_mutex);
    g_pipeline->Reset();
  }

  return env.Undefined();
}

Napi::Value TranscribeFloat32Async(const Napi::CallbackInfo &info) {
  const Napi::Env env = info.Env();

  // Expect: (samples, options, callback)
  if (info.Length() < 3 || !info[2].IsFunction()) {
    Napi::TypeError::New(env, "Expected callback as third argument")
        .ThrowAsJavaScriptException();
    return env.Null();
  }

  Napi::Float32Array input;
  uint32_t sample_rate = 16000;
  uint32_t channels = 1;
  std::string language = state.language; // Default to state language
  std::string session_id; // NEW: Session ID for per-context transcription
  size_t threads = state.threads;

  if (!FillInput(env, info[0], &input, &sample_rate, &channels)) {
    Napi::TypeError::New(env, "First argument must be Float32Array")
        .ThrowAsJavaScriptException();
    return env.Null();
  }

  // Parse options if provided (including language override and sessionId)
  if (info.Length() > 1 && info[1].IsObject()) {
    ParseOptions(env, info[1], &sample_rate, &channels, &language);

    // NEW: Extract sessionId from options
    Napi::Object opts = info[1].As<Napi::Object>();
    if (opts.Has("sessionId") && opts.Get("sessionId").IsString()) {
      session_id = opts.Get("sessionId").ToString().Utf8Value();
    }
  }

  if (!session_id.empty()) {
    auto session_ctx = GetSession(session_id);
    if (!session_ctx || !session_ctx->ctx) {
      Napi::Error::New(env, "whisper session context not found: " + session_id)
          .ThrowAsJavaScriptException();
      return env.Null();
    }
    if (language.empty()) {
      language = session_ctx->language;
    }
    threads = session_ctx->threads;
  } else if (!state.initialized) {
    Napi::Error::New(env, "whisper_addon not initialized")
        .ThrowAsJavaScriptException();
    return env.Null();
  }

  // Prepare audio data
  const size_t frames = input.ElementLength() / std::max<uint32_t>(1, channels);

  std::vector<float> mono = MixToMono(input.Data(), frames, channels);

  std::vector<float> resampled = ResampleLinear(mono, sample_rate, 16000);

  // Create and queue async worker with session_id for parallel transcription
  Napi::Function callback = info[2].As<Napi::Function>();
  auto *worker =
      new TranscribeAsyncWorker(callback, std::move(resampled),
                                language, // Use parsed language from options
                                          // (or default to state.language)
                                threads,
                                session_id); // NEW: Pass session ID
  worker->Queue();

  return env.Undefined();
}

// Returns an array of all registered GGML backend devices with name, type,
// description, and memory info. Lets JS know whether GPU is active.
//
// Return shape: Array<{ name: string, type: "gpu"|"igpu"|"accel"|"cpu",
//                       description: string, memFree: number, memTotal: number }>
Napi::Value GetBackendDevices(const Napi::CallbackInfo &info) {
  Napi::Env env = info.Env();
  Napi::Array devices = Napi::Array::New(env);

  size_t count = ggml_backend_dev_count();
  for (size_t i = 0; i < count; i++) {
    ggml_backend_dev_t dev = ggml_backend_dev_get(i);

    const char *name = ggml_backend_dev_name(dev);
    const char *desc = ggml_backend_dev_description(dev);
    enum ggml_backend_dev_type dev_type = ggml_backend_dev_type(dev);

    size_t mem_free = 0, mem_total = 0;
    ggml_backend_dev_memory(dev, &mem_free, &mem_total);

    const char *type_str;
    switch (dev_type) {
      case GGML_BACKEND_DEVICE_TYPE_GPU:   type_str = "gpu";   break;
      case GGML_BACKEND_DEVICE_TYPE_IGPU:  type_str = "igpu";  break;
      case GGML_BACKEND_DEVICE_TYPE_ACCEL: type_str = "accel"; break;
      default:                             type_str = "cpu";   break;
    }

    Napi::Object obj = Napi::Object::New(env);
    obj.Set("name",        Napi::String::New(env, name ? name : ""));
    obj.Set("type",        Napi::String::New(env, type_str));
    obj.Set("description", Napi::String::New(env, desc ? desc : ""));
    obj.Set("memFree",     Napi::Number::New(env, static_cast<double>(mem_free)));
    obj.Set("memTotal",    Napi::Number::New(env, static_cast<double>(mem_total)));
    devices.Set(i, obj);
  }

  return devices;
}

Napi::Object Init(Napi::Env env, Napi::Object exports) {
  exports.Set("initialize", Napi::Function::New(env, Initialize));
  exports.Set("transcribeFloat32", Napi::Function::New(env, TranscribeFloat32));
  exports.Set("transcribeFloat32Async",
              Napi::Function::New(env, TranscribeFloat32Async));
  exports.Set("processAudio", Napi::Function::New(env, ProcessAudio));
  exports.Set("flushAudio", Napi::Function::New(env, FlushAudio));
  exports.Set("resetPipeline", Napi::Function::New(env, ResetPipeline));
  exports.Set("dispose", Napi::Function::New(env, Dispose));

  // Session management for parallel transcription
  exports.Set("initializeSession", Napi::Function::New(env, InitializeSession));
  exports.Set("destroySession", Napi::Function::New(env, DestroySession));

  exports.Set("getBackendDevices", Napi::Function::New(env, GetBackendDevices));

  return exports;
}
} // namespace

NODE_API_MODULE(whisper_addon, Init)
