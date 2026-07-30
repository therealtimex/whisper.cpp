#include "audio_pipeline.h"
#include "whisper.h" // For VAD functions
#include <algorithm>
#include <cmath>
#include <cstdio>

namespace audio {

inline float ClampSample(float v) { return std::max(-1.0f, std::min(1.0f, v)); }

AudioPipeline::AudioPipeline(const PipelineConfig &config) : config_(config) {
  // Calculate pre-speech buffer size in samples (mono, 16kHz)
  maxPreSpeechSamples_ = static_cast<size_t>(
      (config_.outputSampleRate * config_.preSpeechPadMs) / 1000.0);

  // Calculate minimum segment size in samples (mono, 16kHz)
  minSegmentSamples_ = static_cast<size_t>(
      (config_.outputSampleRate * config_.minSegmentMs) / 1000.0);

  // Calculate max buffer size for Whisper VAD (~30s of audio)
  maxBufferSamples_ = config_.outputSampleRate * 30;

}

AudioPipeline::~AudioPipeline() {
  // Note: We don't free vad_ctx_ here because it's owned by whisper_addon
}

void AudioPipeline::SetVADContext(whisper_vad_context *vad_ctx) {
  vad_ctx_ = vad_ctx;
}

std::vector<float> AudioPipeline::MixToMono(const float *data, size_t frames) {
  if (config_.inputChannels <= 1) {
    return std::vector<float>(data, data + frames);
  }

  std::vector<float> mono(frames);
  const uint32_t ch = config_.inputChannels;
  for (size_t i = 0; i < frames; ++i) {
    float sum = 0.0f;
    const size_t base = i * ch;
    for (uint32_t c = 0; c < ch; ++c) {
      sum += data[base + c];
    }
    mono[i] = sum / static_cast<float>(ch);
  }
  return mono;
}

std::vector<float> AudioPipeline::Resample(const std::vector<float> &input) {
  const uint32_t inRate = config_.inputSampleRate;
  const uint32_t outRate = config_.outputSampleRate;

  if (inRate == outRate) {
    return input;
  }

  // Combine with remainder from previous call
  std::vector<float> combined;
  if (!resampleRemainder_.empty()) {
    combined.reserve(resampleRemainder_.size() + input.size());
    combined.insert(combined.end(), resampleRemainder_.begin(),
                    resampleRemainder_.end());
    combined.insert(combined.end(), input.begin(), input.end());
  } else {
    combined = input;
  }

  if (combined.size() < 2) {
    resampleRemainder_ = combined;
    return {};
  }

  const double ratio =
      static_cast<double>(inRate) / static_cast<double>(outRate);
  const size_t maxOutFrames =
      static_cast<size_t>(std::floor(combined.size() / ratio));

  if (maxOutFrames == 0) {
    resampleRemainder_ = combined;
    return {};
  }

  std::vector<float> output(maxOutFrames);
  size_t lastBase = 0;

  for (size_t i = 0; i < maxOutFrames; ++i) {
    const double pos = static_cast<double>(i) * ratio;
    const size_t base = static_cast<size_t>(pos);
    const size_t next = std::min(base + 1, combined.size() - 1);
    const double frac = pos - static_cast<double>(base);
    output[i] = ClampSample(static_cast<float>(combined[base] * (1.0 - frac) +
                                               combined[next] * frac));
    lastBase = base;
  }

  // Store remainder for next call
  const size_t consumed = std::min(lastBase + 2, combined.size());
  if (consumed < combined.size()) {
    resampleRemainder_.assign(combined.begin() + consumed, combined.end());
  } else {
    resampleRemainder_.clear();
  }

  return output;
}

AudioPipeline::VADFrame
AudioPipeline::ProcessVAD(const std::vector<float> &chunk) {
  if (chunk.empty()) {
    return {false, 0.0f, 0.0};
  }

  // Calculate duration
  const double durationMs =
      (static_cast<double>(chunk.size()) / config_.outputSampleRate) * 1000.0;

  // Calculate RMS energy
  double sumSquares = 0.0;
  for (float sample : chunk) {
    sumSquares += sample * sample;
  }
  const float rms = static_cast<float>(std::sqrt(sumSquares / chunk.size()));

  // Update VAD state machine
  const bool isAbove = rms >= config_.vadEnergyThreshold;

  if (isAbove) {
    vadSpeechMs_ += durationMs;
    vadSilenceMs_ = 0.0;
    if (!vadSpeech_ && vadSpeechMs_ >= config_.vadAttackMs) {
      vadSpeech_ = true;
    }
  } else {
    vadSilenceMs_ += durationMs;
    vadSpeechMs_ = 0.0;
    if (vadSpeech_ && vadSilenceMs_ >= config_.vadReleaseMs) {
      vadSpeech_ = false;
    }
  }

  return {vadSpeech_, rms, durationMs};
}

std::vector<AudioSegment> AudioPipeline::Process(const float *data,
                                                 size_t frames) {
  std::vector<AudioSegment> segments;

  if (!data || frames == 0) {
    return segments;
  }

  // Step 1: Mix to mono
  std::vector<float> mono = MixToMono(data, frames);

  // Step 2: Resample to 16kHz
  std::vector<float> resampled = Resample(mono);

  if (resampled.empty()) {
    return segments; // Not enough data yet
  }

  // NOTE: Whisper VAD maintains cumulative state and returns absolute
  // timestamps, which is not suitable for streaming/chunked processing. We use
  // simple energy-based VAD for segmentation instead. Whisper VAD context is
  // kept for potential future use (e.g., post-processing).

  // Step 3: Run VAD
  VADFrame vadFrame = ProcessVAD(resampled);

  // Step 4: Segmentation logic
  if (vadFrame.isSpeech) {
    if (!activeSpeech_) {
      // Speech just started
      activeSpeech_ = true;
      silenceMs_ = 0.0;
      PrimeWithPreSpeech();
    }

    AppendToCurrentSegment(resampled);

    // Check if we've hit max segment duration
    if (CurrentSegmentDurationMs() >= config_.maxSegmentMs) {
      AudioSegment seg = FlushCurrentSegment(true);
      if (!seg.samples.empty()) {
        segments.push_back(std::move(seg));
      }
      activeSpeech_ = false;
      silenceMs_ = 0.0;
    }
  } else {
    // No speech detected
    if (!activeSpeech_) {
      // Still waiting for speech - add to pre-speech buffer
      AddToPreSpeechBuffer(resampled);
      return segments;
    }

    // We were in speech, now in silence
    silenceMs_ += vadFrame.durationMs;
    AppendToCurrentSegment(resampled);

    // Check if silence has lasted long enough to end the segment
    if (silenceMs_ >= config_.postSpeechPadMs) {
      AudioSegment seg = FlushCurrentSegment(false);
      if (!seg.samples.empty()) {
        segments.push_back(std::move(seg));
      }
      activeSpeech_ = false;
      silenceMs_ = 0.0;
    }
  }

  return segments;
}

std::vector<AudioSegment> AudioPipeline::ProcessWithWhisperVAD() {
  std::vector<AudioSegment> segments;

  if (!vad_ctx_ || continuousBuffer_.empty()) {
    return segments;
  }

  // Set up VAD parameters
  whisper_vad_params vad_params = whisper_vad_default_params();
  vad_params.threshold = 0.5f; // Speech probability threshold
  vad_params.min_speech_duration_ms = static_cast<int>(config_.minSpeechMs);
  vad_params.min_silence_duration_ms =
      static_cast<int>(config_.postSpeechPadMs);
  vad_params.max_speech_duration_s =
      static_cast<float>(config_.maxSegmentMs) / 1000.0f;
  vad_params.speech_pad_ms = static_cast<int>(config_.preSpeechPadMs);

  // Run Whisper VAD to get segments
  whisper_vad_segments *vad_segments = whisper_vad_segments_from_samples(
      vad_ctx_, vad_params, continuousBuffer_.data(),
      static_cast<int>(continuousBuffer_.size()));

  if (!vad_segments) {
    return segments;
  }

  // Extract segments
  const int n_segments = whisper_vad_segments_n_segments(vad_segments);

  for (int i = 0; i < n_segments; ++i) {
    const float t0 = whisper_vad_segments_get_segment_t0(vad_segments, i);
    const float t1 = whisper_vad_segments_get_segment_t1(vad_segments, i);

    const size_t start_sample =
        static_cast<size_t>(t0 * config_.outputSampleRate);
    const size_t end_sample =
        static_cast<size_t>(t1 * config_.outputSampleRate);

    // Validate bounds - t0/t1 are relative to start of buffer
    if (start_sample < end_sample && start_sample < continuousBuffer_.size() &&
        end_sample <= continuousBuffer_.size()) {

      AudioSegment segment;
      segment.samples.assign(continuousBuffer_.begin() + start_sample,
                             continuousBuffer_.begin() + end_sample);
      segment.durationMs = (t1 - t0) * 1000.0;


      segments.push_back(std::move(segment));
    }
  }

  // Free VAD segments
  whisper_vad_free_segments(vad_segments);

  // Clear processed buffer (keep last 2s for overlap)
  const size_t keepSamples = config_.outputSampleRate * 2;
  if (continuousBuffer_.size() > keepSamples) {
    const size_t toRemove = continuousBuffer_.size() - keepSamples;
    continuousBuffer_.erase(continuousBuffer_.begin(),
                            continuousBuffer_.begin() + toRemove);
  }

  return segments;
}

std::vector<AudioSegment> AudioPipeline::Flush() {
  std::vector<AudioSegment> segments;

  // Flush any pending segment from simple VAD
  if (currentSamples_ > 0) {
    AudioSegment seg = FlushCurrentSegment(true);
    if (!seg.samples.empty()) {
      segments.push_back(std::move(seg));
    }
  }

  activeSpeech_ = false;
  silenceMs_ = 0.0;
  vadSpeech_ = false;
  vadSpeechMs_ = 0.0;
  vadSilenceMs_ = 0.0;
  continuousBuffer_.clear();

  return segments;
}

void AudioPipeline::AppendToCurrentSegment(const std::vector<float> &chunk) {
  if (chunk.empty())
    return;

  currentChunks_.push_back(chunk);
  currentSamples_ += chunk.size();
}

void AudioPipeline::AddToPreSpeechBuffer(const std::vector<float> &chunk) {
  if (chunk.empty() || maxPreSpeechSamples_ == 0)
    return;

  preSpeechBuffer_.push_back(chunk);
  preSpeechSampleCount_ += chunk.size();

  // Remove old chunks if buffer is too large
  while (preSpeechSampleCount_ > maxPreSpeechSamples_ &&
         !preSpeechBuffer_.empty()) {
    preSpeechSampleCount_ -= preSpeechBuffer_.front().size();
    preSpeechBuffer_.erase(preSpeechBuffer_.begin());
  }
}

void AudioPipeline::PrimeWithPreSpeech() {
  if (preSpeechBuffer_.empty())
    return;

  for (const auto &chunk : preSpeechBuffer_) {
    currentChunks_.push_back(chunk);
    currentSamples_ += chunk.size();
  }

  preSpeechBuffer_.clear();
  preSpeechSampleCount_ = 0;
}

AudioSegment AudioPipeline::FlushCurrentSegment(bool force) {
  AudioSegment segment;

  if (currentSamples_ == 0) {
    ResetCurrentSegment();
    return segment;
  }

  const double durationMs = CurrentSegmentDurationMs();

  // Check minimum requirements (unless forced)
  if (!force) {
    if (durationMs < config_.minSegmentMs ||
        currentSamples_ < minSegmentSamples_) {
      ResetCurrentSegment();
      return segment;
    }
  }

  // Concatenate all chunks into a single buffer
  segment.samples.reserve(currentSamples_);
  for (const auto &chunk : currentChunks_) {
    segment.samples.insert(segment.samples.end(), chunk.begin(), chunk.end());
  }
  segment.durationMs = durationMs;

  ResetCurrentSegment();
  return segment;
}

void AudioPipeline::ResetCurrentSegment() {
  currentChunks_.clear();
  currentSamples_ = 0;
}

double AudioPipeline::CurrentSegmentDurationMs() const {
  if (currentSamples_ == 0)
    return 0.0;
  return (static_cast<double>(currentSamples_) / config_.outputSampleRate) *
         1000.0;
}

void AudioPipeline::Reset() {
  // Clear all buffers
  currentChunks_.clear();
  currentSamples_ = 0;

  preSpeechBuffer_.clear();
  preSpeechSampleCount_ = 0;

  resampleRemainder_.clear();
  continuousBuffer_.clear();

  // Reset VAD state
  vadSpeech_ = false;
  vadSpeechMs_ = 0.0;
  vadSilenceMs_ = 0.0;

  // Reset segmentation state
  activeSpeech_ = false;
  silenceMs_ = 0.0;

}

} // namespace audio
