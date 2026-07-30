#ifndef AUDIO_PIPELINE_H
#define AUDIO_PIPELINE_H

#include <cstdint>
#include <memory>
#include <vector>

// Forward declaration for Whisper VAD
struct whisper_vad_context;

namespace audio {

struct AudioSegment {
  std::vector<float> samples; // mono, 16kHz
  double durationMs;
};

struct PipelineConfig {
  uint32_t inputSampleRate = 48000;
  uint32_t outputSampleRate = 16000;
  uint32_t inputChannels = 2;

  // VAD parameters
  float vadEnergyThreshold = 0.01f;
  uint32_t vadAttackMs = 120;
  uint32_t vadReleaseMs = 260;

  // Segmentation parameters
  uint32_t minSpeechMs = 250;
  uint32_t minSegmentMs = 250;
  uint32_t maxSegmentMs = 30000;
  uint32_t preSpeechPadMs = 300;
  uint32_t postSpeechPadMs = 400;
};

class AudioPipeline {
public:
  explicit AudioPipeline(const PipelineConfig &config);
  ~AudioPipeline();

  // Set Whisper VAD context (optional, for ML-based VAD)
  void SetVADContext(whisper_vad_context *vad_ctx);

  // Process a chunk of raw audio (interleaved, input sample rate/channels)
  // Returns any completed speech segments
  std::vector<AudioSegment> Process(const float *data, size_t frames);

  // Flush any pending audio and return final segment if exists
  std::vector<AudioSegment> Flush();

  // Reset all internal state between recording sessions
  void Reset();

private:
  // Audio processing helpers
  std::vector<float> MixToMono(const float *data, size_t frames);
  std::vector<float> Resample(const std::vector<float> &input);

  // VAD helpers
  struct VADFrame {
    bool isSpeech;
    float rms;
    double durationMs;
  };
  VADFrame ProcessVAD(const std::vector<float> &chunk);

  // Whisper VAD-based segmentation
  std::vector<AudioSegment> ProcessWithWhisperVAD();

  // Segmentation helpers
  void AppendToCurrentSegment(const std::vector<float> &chunk);
  void AddToPreSpeechBuffer(const std::vector<float> &chunk);
  void PrimeWithPreSpeech();
  AudioSegment FlushCurrentSegment(bool force = false);
  void ResetCurrentSegment();
  double CurrentSegmentDurationMs() const;

  PipelineConfig config_;

  // Whisper VAD context (optional)
  whisper_vad_context *vad_ctx_ = nullptr;

  // VAD state
  bool vadSpeech_ = false;
  double vadSpeechMs_ = 0.0;
  double vadSilenceMs_ = 0.0;

  // Segmentation state
  std::vector<std::vector<float>> currentChunks_;
  size_t currentSamples_ = 0;
  bool activeSpeech_ = false;
  double silenceMs_ = 0.0;

  // Pre-speech buffer
  std::vector<std::vector<float>> preSpeechBuffer_;
  size_t preSpeechSampleCount_ = 0;
  size_t maxPreSpeechSamples_ = 0;
  size_t minSegmentSamples_ = 0;

  // Resampling state
  std::vector<float> resampleRemainder_;

  // Buffer for continuous audio (for Whisper VAD)
  std::vector<float> continuousBuffer_;
  size_t maxBufferSamples_ = 0; // Max samples to buffer (~30s)
};

} // namespace audio

#endif // AUDIO_PIPELINE_H
