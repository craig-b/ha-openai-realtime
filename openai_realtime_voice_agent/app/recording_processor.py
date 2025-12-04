"""Frame processor for audio recording integration with Pipecat."""
import logging
from typing import Optional
from pipecat.frames.frames import (
    AudioRawFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    Frame,
    EndFrame,
    StartFrame
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from app.audio_recorder import AudioRecorder

logger = logging.getLogger(__name__)


class AudioRecordingProcessor(FrameProcessor):
    """Frame processor that records audio frames for debugging.
    
    This processor can be placed in the pipeline to record either input or output audio.
    Use record_input=True for input audio (before OpenAI service) and record_input=False
    for output audio (after OpenAI service).
    """
    
    def __init__(
        self,
        enable_recording: bool = True,
        client_id: Optional[str] = None,
        record_input: bool = True,
        shared_recorder: Optional[AudioRecorder] = None,
        **kwargs
    ):
        # Initialize FrameProcessor first (pass kwargs to allow custom initialization)
        super().__init__(**kwargs)
        self.enable_recording = enable_recording
        self.client_id = client_id or "unknown"
        self.record_input = record_input  # True for input audio, False for output audio
        self.recorder: Optional[AudioRecorder] = shared_recorder
        
        # Only create recorder if not shared and recording is enabled
        if self.enable_recording and not self.recorder:
            self.recorder = AudioRecorder()
            self.recorder.start_recording(client_id=self.client_id)
            logger.info(f"🎙️ Started audio recording for client {self.client_id}")
    
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and record audio."""
        # For StartFrame, let the parent handle lifecycle tracking first
        # This sets __started = True so subsequent frames can be processed
        if isinstance(frame, StartFrame):
            logger.debug(f"🎬 AudioRecordingProcessor: Received StartFrame")
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            return
        
        # Handle both AudioRawFrame (legacy) and InputAudioRawFrame/OutputAudioRawFrame (official transport)
        if isinstance(frame, (AudioRawFrame, InputAudioRawFrame, OutputAudioRawFrame)):
            logger.debug(f"🎵 AudioRecordingProcessor: Received {type(frame).__name__} ({len(frame.audio)} bytes)")
            if self.recorder:
                # Record based on processor type (input or output) AND frame type
                audio_bytes = frame.audio
                num_samples = len(audio_bytes) // 2  # 16-bit = 2 bytes per sample
                
                # Determine if this is input or output based on frame type
                # InputAudioRawFrame = always input (only record in input processor)
                # OutputAudioRawFrame = always output (only record in output processor)
                # AudioRawFrame = depends on processor setting (legacy support)
                if isinstance(frame, OutputAudioRawFrame):
                    # OutputAudioRawFrame should only be recorded by output processor
                    if not self.record_input:
                        logger.info(f"🔊 Recording output: {len(audio_bytes)} bytes ({num_samples} samples) - Frame type: {type(frame).__name__}")
                        self.recorder.record_output_audio(audio_bytes)
                    else:
                        logger.debug(f"⚠️ OutputAudioRawFrame received in input processor, skipping")
                elif isinstance(frame, InputAudioRawFrame):
                    # InputAudioRawFrame should only be recorded by input processor
                    if self.record_input:
                        logger.info(f"🎤 Recording input: {len(audio_bytes)} bytes ({num_samples} samples)")
                        self.recorder.record_input_audio(audio_bytes)
                    else:
                        logger.debug(f"⚠️ InputAudioRawFrame received in output processor, skipping")
                elif isinstance(frame, AudioRawFrame):
                    # Legacy: AudioRawFrame - record based on processor setting
                    if self.record_input:
                        logger.info(f"🎤 Recording input (legacy): {len(audio_bytes)} bytes ({num_samples} samples)")
                        self.recorder.record_input_audio(audio_bytes)
                    else:
                        logger.info(f"🔊 Recording output (legacy): {len(audio_bytes)} bytes ({num_samples} samples)")
                        self.recorder.record_output_audio(audio_bytes)
        elif isinstance(frame, EndFrame) and self.recorder and self.record_input:
            # Only stop recording once (from the input processor)
            # Stop recording when connection ends
            self.recorder.stop_recording()
            self.recorder = None
            logger.info(f"✅ Stopped audio recording for client {self.client_id}")
        
        # Pass frame through
        logger.debug(f"📤 AudioRecordingProcessor: Pushing {type(frame).__name__} to next processor")
        await self.push_frame(frame, direction)
    
    def cleanup(self):
        """Cleanup resources."""
        if self.recorder and self.record_input:
            # Only cleanup from input processor
            self.recorder.stop_recording()
            self.recorder = None

