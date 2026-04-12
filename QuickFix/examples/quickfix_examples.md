# QuickFix — Usage Examples

## List compatible plugins
python cli/cli.py list --file example.txt

## Show plugin details
python cli/cli.py info --plugin reverse_text_phrases

## Run plugin, keep output alongside original
python cli/cli.py run --file example.txt --plugin reverse_text_phrases

## Run plugin and overwrite original
python cli/cli.py run --file example.txt --plugin reverse_text_phrases --save

## Run plugin and save to specific path
python cli/cli.py run --file example.txt --plugin reverse_text_phrases --save-as out.txt

## Interactive mode
python cli/cli.py --menu
./run.sh --cli
