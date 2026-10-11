from activity_format import format_activity_answer


def test_without_citations() -> None:
    assert format_activity_answer("Answer", []) == "Answer"


def test_with_linked_and_plain_citations() -> None:
    actual = format_activity_answer(
        "Answer",
        [
            {
                "label": "Global policy",
                "location": "Page 4",
                "url": "https://example.test/global",
            },
            {
                "label": "India policy",
                "location": None,
                "url": None,
            },
        ],
    )
    assert actual == (
        "Answer\n\nSources:\n"
        "- [Global policy - Page 4](https://example.test/global)\n"
        "- India policy"
    )


if __name__ == "__main__":
    test_without_citations()
    test_with_linked_and_plain_citations()
    print("Activity formatting tests passed.")
