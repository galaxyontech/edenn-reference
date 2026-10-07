from EdennCode.WorkflowFactory.VideoVoiceOverWorkflow.Stages.PreprocessStage.preprocess_stage import PreprocessStage, PreprocessStageInput, PreprocessStageOutput
from pathlib import Path

class PreprocessStageTest:
    @staticmethod
    def preprocess_stage_test_1():
        voice_over_preprocessor =  PreprocessStage()
        voice_over_preprocessor_stage_input = PreprocessStageInput(video_path= Path("temp.mp4"))
        print(voice_over_preprocessor_stage_input)
        output = voice_over_preprocessor.run(voice_over_preprocessor_stage_input)
        print(output)


if __name__ == "__main__":
    test_stage = PreprocessStageTest()
    test_stage.preprocess_stage_test_1()
