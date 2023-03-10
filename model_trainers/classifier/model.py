import torch
import os

from tqdm.auto import tqdm
from transformers import RobertaTokenizerFast, RobertaForTokenClassification, get_scheduler

from pathlib import Path
from datasets import Dataset

from torch.utils.data import DataLoader
from torch.optim import AdamW
from torch.nn import BCEWithLogitsLoss

# pylint: disable-next=relative-beyond-top-level
from ..utils import get_device, make_dir_if_not_exists, get_class_weights, evaluate_model, multi_hot_vector_to_class_vector

from sklearn.metrics import classification_report

MODEL_FOLDER = Path(__file__).parent.parent.parent.resolve() / "models"


def train_classifier(dataset: Dataset, model_name: str, num_epochs: int = 3, weighted: bool = False):

    # uncomment for local testing
    # dataset["train"] = dataset["train"].select(range(64))
    # dataset["test"] = dataset["test"].select(range(64))

    label_list = dataset["train"].features["verbnet"].feature.feature.names

    model = RobertaForTokenClassification.from_pretrained(
        "roberta-base", num_labels=len(label_list),
        id2label={i: l for i, l in enumerate(label_list)},
        label2id={l: i for i, l in enumerate(label_list)}
    )

    tokenizer = RobertaTokenizerFast.from_pretrained(
        "roberta-base", add_prefix_space=True)

    tokenized_data = _tokenize_data(dataset, tokenizer, label_list)

    device = get_device()
    print(f"### training model on {device} ###")
    model.to(device)

    train_dataloader = DataLoader(
        tokenized_data["train"], shuffle=True, batch_size=1)

    eval_dataloader = DataLoader(
        tokenized_data["test"], shuffle=True, batch_size=1)

    optimizer = AdamW(model.parameters(), lr=2e-5)

    num_training_steps = num_epochs * len(train_dataloader)

    lr_scheduler = get_scheduler(
        name="linear", optimizer=optimizer, num_warmup_steps=0, num_training_steps=num_training_steps
    )

    loss_weights = torch.ones(len(label_list), device=device)
    if weighted:
        loss_weights = get_class_weights(dataset["train"], label_list)

    loss_fct = BCEWithLogitsLoss(pos_weight=loss_weights)

    progress_bar = tqdm(range(num_training_steps))

    for epoch in range(num_epochs):

        model.train()

        for batch in train_dataloader:
            running_loss = 0.0
            labels = batch.pop("labels")
            batch.pop("input_text")

            batch = {k: v.to(device) for k, v in batch.items()}

            outputs = model(**batch)

            ignore_index = labels.mean(-1).squeeze().int()

            flat_outputs = outputs.logits.squeeze()[ignore_index != -100]
            flat_labels = labels.squeeze()[ignore_index != -100]

            flat_outputs = flat_outputs.to(device)
            flat_labels = flat_labels.to(device)

            loss = loss_fct(flat_outputs, flat_labels)
            loss.backward()

            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()

            running_loss += loss.item() * labels.size(0)
            progress_bar.update(1)

        epoch_loss = running_loss / len(train_dataloader)
        progress_bar.write(f"Epoch {epoch}, Loss: {epoch_loss}")

        model.eval()

        preds = []
        true_labels = []

        for batch in eval_dataloader:

            labels = batch.pop("labels")
            batch.pop("input_text")

            batch = {k: v.to(device) for k, v in batch.items()}

            with torch.no_grad():
                outputs = model(**batch)

            ignore_index = labels.mean(-1).squeeze().int()

            flat_outputs = outputs.logits.squeeze()[ignore_index != -100]
            flat_labels = labels.squeeze()[ignore_index != -100]

            pred = flat_outputs.heaviside(torch.tensor(
                [0.0], device=device)).int().tolist()
            true_label = flat_labels.int().tolist()

            preds.extend(pred)
            true_labels.extend(true_label)

        # preds = multi_hot_vector_to_class_vector(preds)
        # true_labels = multi_hot_vector_to_class_vector(true_labels)

        metrics = classification_report(
            y_true=true_labels, y_pred=preds, target_names=label_list, zero_division=0, output_dict=True)

        progress_bar.write("micro average: " + str(metrics["micro avg"]))
        progress_bar.write("macro average: " + str(metrics["macro avg"]))
        progress_bar.write("weighted average: " + str(metrics["weighted avg"]))
        progress_bar.write("samples average: " + str(metrics["samples avg"]))

    progress_bar.close()

    make_dir_if_not_exists(MODEL_FOLDER)

    model.save_pretrained(MODEL_FOLDER / "srl-classifier" / model_name)


def evaluate_classifier(dataset: Dataset, model_name: str):

    # uncomment for local testing
    # dataset["train"] = dataset["train"].select(range(64))
    # dataset["test"] = dataset["test"].select(range(64))

    label_list = dataset["train"].features["verbnet"].feature.feature.names

    tokenizer = RobertaTokenizerFast.from_pretrained(
        "roberta-base", add_prefix_space=True)

    tokenized_data = _tokenize_data(dataset, tokenizer, label_list)

    model = RobertaForTokenClassification.from_pretrained(
        MODEL_FOLDER / "srl-classifier" / model_name,  num_labels=len(label_list))

    device = get_device()
    print(f"### evaluating model on {device} ###")
    model.to(device)

    eval_dataloader = DataLoader(
        tokenized_data["test"], shuffle=False, batch_size=1)

    model.eval()

    preds = []
    true_labels = []
    inputs = []

    progress_bar = tqdm(range(len(eval_dataloader)))

    for batch in eval_dataloader:

        labels = batch.pop("labels")
        inputs.append(batch.pop("input_text"))

        batch = {k: v.to(device) for k, v in batch.items()}

        with torch.no_grad():
            outputs = model(**batch)

        ignore_index = labels.mean(-1).squeeze().int()

        flat_outputs = outputs.logits.squeeze()[ignore_index != -100]
        flat_labels = labels.squeeze()[ignore_index != -100]

        pred = flat_outputs.heaviside(torch.tensor(
            [0.0], device=device)).int().tolist()
        true_label = flat_labels.int().tolist()

        preds.append(multi_hot_vector_to_class_vector(pred))
        true_labels.append(multi_hot_vector_to_class_vector(true_label))

        progress_bar.update(1)

    progress_bar.close()

    evaluate_model(dataset, inputs, true_labels, preds)


def _tokenize_data(dataset: Dataset, tokenizer: RobertaTokenizerFast, label_list: list, label_all_tokens: bool = True) -> Dataset:

    label_count = len(label_list)

    def tokenize_and_align(examples):
        tokens = tokenizer(
            examples["tok"], is_split_into_words=True, truncation=True)

        labels = []
        for i, label in enumerate(examples["verbnet"]):
            word_ids = tokens.word_ids(batch_index=i)
            previous_word_idx = None
            label_ids = []
            for word_idx in word_ids:

                if word_idx is None:
                    label_ids.append([-100 for l in range(label_count)])
                elif word_idx != previous_word_idx:
                    # only set a single label for now
                    label_ids.append(
                        [1.0 if l in label[word_idx] else 0.0 for l in range(label_count)])
                else:
                    label_ids.append([1.0 if l in label[word_idx] else 0.0 for l in range(label_count)]
                                     if label_all_tokens else [-100 for l in range(label_count)])

                previous_word_idx = word_idx

            labels.append(label_ids)

        tokens["labels"] = labels
        tokens["input_text"] = examples["tok"]

        return tokens

    tokenized_dataset = dataset.map(tokenize_and_align, batched=True, num_proc=os.cpu_count(
    ), remove_columns=dataset["test"].column_names)

    tokenized_dataset.set_format(
        "torch", columns=["input_ids", "attention_mask", "labels"], output_all_columns=True)

    return tokenized_dataset
